"""Versioned, fail-closed contract for the owner's n8n workflow.

The workflow and its provider credentials live outside this application.  No
Telegram update consumer or provider-specific paid call is created here.
"""

import asyncio
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

SCHEMA_VERSION = "mika.n8n.v1"
TASKS = frozenset({"tts", "stt", "chat", "image_generate", "image_analyze"})
STATUSES = frozenset({"succeeded", "accepted", "pending", "running", "failed",
                      "cancelled", "expired"})
RESULT_KINDS = frozenset({"text", "photo", "audio", "voice", "document",
                          "json_transcript", "error", "catalog", "upload"})


class GatewayError(ValueError):
    """Safe, non-provider-specific error for a Telegram or owner status screen."""


def _endpoint(base_url: str, webhook_path: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username \
            or parsed.password or parsed.query or parsed.fragment:
        raise GatewayError("n8n endpoint is invalid")
    if parsed.scheme == "http":
        try:
            if not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise GatewayError("n8n HTTP is allowed only on loopback")
        except ValueError:
            raise GatewayError("n8n requires HTTPS outside loopback") from None
    if not webhook_path or not webhook_path.startswith("/") or webhook_path.startswith("//") \
            or "?" in webhook_path or "#" in webhook_path or ".." in webhook_path:
        raise GatewayError("n8n webhook path is invalid")
    return base_url.rstrip("/") + webhook_path


def _secret_from_ref(ref: str) -> str:
    path = Path(ref).expanduser()
    if path.is_symlink() or not path.is_file():
        raise GatewayError("n8n auth file is missing")
    stat = path.stat()
    if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise GatewayError("n8n auth file must be owned by the bot user and mode 0600")
    secret = path.read_text(encoding="utf-8").strip()
    if not 32 <= len(secret) <= 512 or "\n" in secret or "\r" in secret:
        raise GatewayError("n8n auth file has an invalid token")
    return secret


def _validate_response(payload: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION \
            or payload.get("request_id") != request_id or payload.get("status") not in STATUSES:
        raise GatewayError("n8n returned an incompatible response")
    status = payload["status"]
    if status == "accepted" and not isinstance(payload.get("job_id"), str):
        raise GatewayError("n8n accepted a job without an ID")
    if status == "succeeded":
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("kind") not in RESULT_KINDS:
            raise GatewayError("n8n returned an invalid result")
        if result["kind"] in {"audio", "voice", "photo", "document"} and not \
                isinstance(result.get("file_ref"), str):
            raise GatewayError("n8n returned media without a private file reference")
        if result["kind"] == "catalog" and not isinstance(result.get("items"), list):
            raise GatewayError("n8n returned an invalid catalog")
        if result["kind"] == "upload" and not isinstance(result.get("file_ref"), str):
            raise GatewayError("n8n did not return a private upload reference")
    if status == "failed" and not isinstance(payload.get("error"), dict):
        raise GatewayError("n8n failed without a structured error")
    return payload


class N8NGateway:
    def __init__(self, *, enabled: bool, base_url: str = "", webhook_path: str = "",
                 auth_ref: str = "", timeout: float = 12, max_parallel: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.enabled = enabled
        self.url = _endpoint(base_url, webhook_path) if enabled else ""
        self.auth_ref = auth_ref
        self.timeout = timeout
        self.slots = asyncio.Semaphore(max_parallel)
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False,
            limits=httpx.Limits(max_connections=max_parallel,
                                max_keepalive_connections=max_parallel), transport=transport)
        self._catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        if enabled and (not auth_ref or not 2 <= timeout <= 30 or not 1 <= max_parallel <= 4):
            raise GatewayError("n8n needs private authentication and bounded limits")

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, action: str, *, request_id: str | None = None,
                       input_data: dict[str, Any] | None = None,
                       options: dict[str, Any] | None = None,
                       context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise GatewayError("n8n is not configured")
        request_id = request_id or secrets.token_urlsafe(18)
        payload = {"schema_version": SCHEMA_VERSION, "request_id": request_id,
                   "task": action, "context": context or {}, "input": input_data or {},
                   "options": options or {}}
        headers = {"X-Mikachan-Auth": _secret_from_ref(self.auth_ref),
                   "Content-Type": "application/json"}
        try:
            async with self.slots:
                response = await self.client.post(self.url, json=payload, headers=headers)
            if response.status_code in {401, 403}:
                raise GatewayError("n8n authentication failed")
            if response.status_code == 429:
                raise GatewayError("n8n quota or rate limit reached")
            if response.status_code >= 500:
                raise GatewayError("n8n service is unavailable")
            if response.status_code not in {200, 202}:
                raise GatewayError(f"n8n rejected the request (HTTP {response.status_code})")
            if len(response.content) > 262_144 or "application/json" not in \
                    response.headers.get("content-type", ""):
                raise GatewayError("n8n returned an invalid response format")
            return _validate_response(response.json(), request_id)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GatewayError("n8n did not respond in time") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GatewayError("n8n returned invalid JSON") from exc

    async def get_capabilities(self) -> dict[str, Any]:
        return await self._request("catalog.capabilities")

    async def get_voices(self, query: str = "", cursor: str = "",
                         model_id: str = "") -> dict[str, Any]:
        options = {"query": query[:80], "cursor": cursor[:200], "model_id": model_id[:100]}
        return await self._catalog("catalog.voices", options)

    async def get_models(self, capability: str) -> dict[str, Any]:
        if capability not in TASKS:
            raise GatewayError("Unsupported capability")
        return await self._catalog("catalog.models", {"capability": capability})

    async def _catalog(self, task: str, options: dict[str, Any]) -> dict[str, Any]:
        key = json.dumps([task, options], sort_keys=True)
        cached = self._catalog_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        result = await self._request(task, options=options)
        if result.get("status") == "succeeded" and result.get("result", {}).get("kind") == "catalog":
            if len(self._catalog_cache) >= 32:
                self._catalog_cache.pop(next(iter(self._catalog_cache)))
            self._catalog_cache[key] = (time.monotonic() + 120, result)
        return result

    async def submit(self, task: str, context: dict[str, Any], input_data: dict[str, Any],
                     options: dict[str, Any], request_id: str) -> dict[str, Any]:
        if task not in TASKS:
            raise GatewayError("Unsupported capability")
        return await self._request(task, request_id=request_id, context=context,
                                   input_data=input_data, options=options)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        if not 1 <= len(job_id) <= 128:
            raise GatewayError("Invalid job ID")
        return await self._request("job.status", options={"job_id": job_id})

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        if not 1 <= len(job_id) <= 128:
            raise GatewayError("Invalid job ID")
        return await self._request("job.cancel", options={"job_id": job_id})

    async def upload(self, content: bytes, mime_type: str, request_id: str) -> str:
        """Send one bounded user-selected file to a private workflow endpoint."""
        if not self.enabled:
            raise GatewayError("n8n is not configured")
        if not 1 <= len(content) <= 20_000_000 or mime_type not in {
            "audio/mpeg", "audio/ogg", "audio/mp4", "audio/wav", "video/mp4"}:
            raise GatewayError("Audio file type or size is unsupported")
        manifest = {"schema_version": SCHEMA_VERSION, "request_id": request_id,
                    "task": "file.upload", "input": {"mime_type": mime_type,
                    "size_bytes": len(content)}, "options": {}}
        headers = {"X-Mikachan-Auth": _secret_from_ref(self.auth_ref)}
        try:
            async with self.slots:
                response = await self.client.post(self.url, data={"manifest": json.dumps(manifest)},
                    files={"media": ("audio.bin", content, mime_type)}, headers=headers,
                    timeout=30)
            if response.status_code != 200 or len(response.content) > 100_000 or \
                    "application/json" not in response.headers.get("content-type", ""):
                raise GatewayError("n8n media upload failed")
            payload = _validate_response(response.json(), request_id)
            if payload["status"] != "succeeded" or payload["result"]["kind"] != "upload":
                raise GatewayError("n8n did not accept the media upload")
            ref = payload["result"]["file_ref"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", ref):
                raise GatewayError("n8n returned an invalid private file reference")
            return ref
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GatewayError("n8n media upload timed out") from exc

    async def fetch_file(self, file_ref: str, request_id: str,
                         allowed_mime: set[str]) -> tuple[bytes, str]:
        if not self.enabled or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", file_ref):
            raise GatewayError("Invalid private file reference")
        headers = {"X-Mikachan-Auth": _secret_from_ref(self.auth_ref)}
        payload = {"schema_version": SCHEMA_VERSION, "request_id": request_id,
                   "task": "file.fetch", "input": {"file_ref": file_ref}, "options": {}}
        try:
            async with self.slots:
                async with self.client.stream("POST", self.url, json=payload, headers=headers,
                                              timeout=30) as response:
                    if response.status_code != 200:
                        raise GatewayError("n8n media reference expired or unavailable")
                    mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if mime not in allowed_mime:
                        raise GatewayError("n8n returned an unsupported media type")
                    chunks = bytearray()
                    async for part in response.aiter_bytes(65536):
                        chunks.extend(part)
                        if len(chunks) > 20_000_000:
                            raise GatewayError("n8n media exceeds the safe Telegram limit")
                    if not chunks:
                        raise GatewayError("n8n returned an empty media file")
                    return bytes(chunks), mime
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GatewayError("n8n media fetch timed out") from exc
