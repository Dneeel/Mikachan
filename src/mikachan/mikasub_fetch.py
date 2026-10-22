"""Read subscription documents with a pinned, public-address-only resolver."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import ResolveResult

MAX_SOURCE_BYTES = 4_000_000
_HOST = re.compile(r"[a-z0-9.-]{1,253}\Z")


def source_url(raw: str) -> str:
    if not 10 <= len(raw) <= 4096 or any(ord(char) <= 32 for char in raw):
        raise ValueError("subscription URL length or characters are invalid")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise ValueError("subscription URL is invalid") from None
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password \
            or parsed.fragment or port not in {None, 80, 443}:
        raise ValueError("use a public HTTP(S) subscription URL without credentials in authority")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private network destination refused")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host) or "." not in host or ".." in host:
            raise ValueError("subscription hostname is invalid") from None
    else:
        if not address.is_global:
            raise ValueError("private network destination refused")
    return raw


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[ResolveResult]:
        source_url(f"https://{host}/")
        answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=1), 5)
        if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global
                              for answer in answers):
            raise ValueError("subscription DNS returned a private address")
        return [{"hostname": host, "host": answer[4][0], "port": port,
                 "family": answer[0], "proto": answer[2], "flags": 0}
                for answer in answers]

    async def close(self) -> None:
        pass


async def fetch_subscription(raw: str) -> bytes:
    """Fetch one document only; never connect to proxy endpoints inside it."""
    current = source_url(raw)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False,
                                     limit_per_host=2)
    timeout = aiohttp.ClientTimeout(total=20, connect=6, sock_read=10)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout,
                                         trust_env=False, auto_decompress=True) as session:
            for _ in range(4):
                source_url(current)
                async with session.get(current, allow_redirects=False,
                                       headers={"Accept": "text/plain,application/octet-stream,*/*"}) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "")
                        if not location:
                            raise ValueError("subscription redirect is incomplete")
                        current = source_url(urljoin(current, location))
                        continue
                    if response.status != 200:
                        raise ValueError(f"subscription fetch returned HTTP {response.status}")
                    try:
                        advertised_size = int(response.headers.get("Content-Length", "0") or "0")
                    except ValueError:
                        advertised_size = 0
                    if advertised_size > MAX_SOURCE_BYTES:
                        raise ValueError("subscription document exceeds 4 MB")
                    content = bytearray()
                    async for block in response.content.iter_chunked(65536):
                        content.extend(block)
                        if len(content) > MAX_SOURCE_BYTES:
                            raise ValueError("subscription document exceeds 4 MB")
                    return bytes(content)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # Client exceptions can embed the access-bearing source URL.
        raise ValueError("subscription host temporarily unavailable") from exc
    raise ValueError("subscription redirected too many times")
