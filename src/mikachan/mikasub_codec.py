"""Loss-aware subscription parsing. No proxy connection is ever attempted."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

import yaml

MAX_DOCUMENT = 4_000_000
MAX_NODES = 10_000
MAX_URI = 16_384
MAX_NAME = 160
RAW_SCHEMES = frozenset({
    "vless", "trojan", "ss", "hysteria", "hysteria2", "hy2", "tuic", "socks",
    "socks5", "http", "https", "naive+https", "wireguard", "snell", "anytls",
})
SUPPORTED_STRUCTURED = frozenset({"ss", "trojan"})
NAME_PREFIX = "🏴‍☠️ Maybe It Works "
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{1,30}://", re.I)


@dataclass(frozen=True)
class Node:
    identity: str
    protocol: str
    uri: str
    name: str


@dataclass(frozen=True)
class Parsed:
    nodes: tuple[Node, ...]
    raw: int
    parsed: int
    duplicates: int
    unsupported: int
    errors: int
    source_format: str


def _decode64(value: str) -> bytes:
    compact = re.sub(r"\s+", "", value)
    if not compact or len(compact) > MAX_DOCUMENT * 2 or not re.fullmatch(r"[A-Za-z0-9_+/=-]+", compact):
        raise ValueError("invalid Base64 data")
    return base64.b64decode(compact.replace("-", "+").replace("_", "/") +
                            "=" * (-len(compact) % 4), validate=True)


def _encode64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _node(uri: str) -> Node:
    uri = uri.strip().lstrip("\ufeff")
    if not 4 <= len(uri.encode("utf-8")) <= MAX_URI or any(ord(c) < 32 for c in uri):
        raise ValueError("share URI length or control character")
    match = _SCHEME.match(uri)
    if not match:
        raise ValueError("not a share URI")
    scheme = match.group()[:-3].lower()
    if scheme == "vmess":
        encoded = uri[len(match.group()):].split("#", 1)[0]
        obj = json.loads(_decode64(encoded))
        if not isinstance(obj, dict) or not isinstance(obj.get("add"), str) or not obj.get("id"):
            raise ValueError("invalid VMess JSON")
        name = str(obj.get("ps") or "")[:MAX_NAME]
        canonical = dict(obj)
        canonical.pop("ps", None)
        return Node(_hash("vmess:" + json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":"))), scheme, uri, name)
    if scheme == "ssr":
        encoded = uri[len(match.group()):].split("#", 1)[0]
        decoded = _decode64(encoded).decode("utf-8")
        base, sep, query = decoded.partition("/?")
        if base.count(":") < 5:
            raise ValueError("invalid SSR record")
        fields = query.split("&") if sep else []
        name = ""
        identity_fields = []
        for field in fields:
            if field.startswith("remarks="):
                try:
                    name = _decode64(unquote(field[8:])).decode("utf-8")[:MAX_NAME]
                except (ValueError, UnicodeError):
                    raise ValueError("invalid SSR remarks") from None
            else:
                identity_fields.append(field)
        return Node(_hash("ssr:" + base + "/?" + "&".join(identity_fields)), scheme, uri, name)
    if scheme not in RAW_SCHEMES:
        raise ValueError("unsupported share scheme")
    base, _, label = uri.partition("#")
    if not base[len(match.group()):]:
        raise ValueError("share URI has no endpoint")
    # The fragment is presentation only for the explicitly supported URI types.
    # Keep the authority, query, path and all their original encodings unchanged.
    return Node(_hash(scheme + ":" + base[len(match.group()):]), scheme, uri,
                unquote(label)[:MAX_NAME])


def rename(node: Node, name: str) -> str:
    """Change only the protocol's display field; all connection fields survive."""
    name = name[:MAX_NAME]
    match = _SCHEME.match(node.uri)
    assert match is not None
    if node.protocol == "vmess":
        obj = json.loads(_decode64(node.uri[len(match.group()):].split("#", 1)[0]))
        obj["ps"] = name
        return "vmess://" + _encode64(json.dumps(obj, ensure_ascii=False,
                                    separators=(",", ":")).encode())
    if node.protocol == "ssr":
        decoded = _decode64(node.uri[len(match.group()):].split("#", 1)[0]).decode()
        base, _, query = decoded.partition("/?")
        fields = [field for field in query.split("&") if field and not field.startswith("remarks=")]
        fields.append("remarks=" + _encode64(name.encode()))
        return "ssr://" + _encode64((base + "/?" + "&".join(fields)).encode())
    return node.uri.split("#", 1)[0] + "#" + quote(name, safe="")


def _simple_structured(record: dict, *, singbox: bool = False) -> str:
    if not isinstance(record, dict):
        raise ValueError("structured record invalid")
    kind = str(record.get("type") or "").lower()
    if kind == "shadowsocks":
        kind = "ss"
    if kind not in SUPPORTED_STRUCTURED:
        raise ValueError("structured protocol cannot be exported losslessly")
    host = record.get("server")
    port = record.get("server_port" if singbox else "port")
    if not isinstance(host, str) or not host or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError("structured endpoint invalid")
    if re.search(r"[\s/@?#]", host):
        raise ValueError("structured endpoint ambiguous")
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    name = str(record.get("tag" if singbox else "name") or "")[:MAX_NAME]
    if kind == "ss":
        allowed = {"type", "server", "server_port" if singbox else "port", "tag" if singbox else "name",
                   "method", "password", "cipher"}
        if set(record) - allowed:
            raise ValueError("structured Shadowsocks has unmapped options")
        method = record.get("method") or record.get("cipher")
        password = record.get("password")
        if not isinstance(method, str) or not isinstance(password, str):
            raise ValueError("structured Shadowsocks credentials missing")
        credentials = _encode64(f"{method}:{password}".encode())
        return f"ss://{credentials}@{host}:{port}#{quote(name, safe='')}"
    if kind == "trojan":
        allowed = {"type", "server", "server_port" if singbox else "port", "tag" if singbox else "name",
                   "password", "sni", "skip-cert-verify", "tls"}
        if set(record) - allowed or record.get("skip-cert-verify") or record.get("tls") not in (None, True):
            raise ValueError("structured Trojan has unmapped options")
        password = record.get("password")
        if not isinstance(password, str) or not password:
            raise ValueError("structured Trojan password missing")
        sni = record.get("sni")
        query = "?sni=" + quote(sni, safe="") if isinstance(sni, str) and sni else ""
        return f"trojan://{quote(password, safe='')}@{host}:{port}{query}#{quote(name, safe='')}"
    raise ValueError("structured protocol has parameters requiring native export")


def _records(document: str, depth: int = 0) -> tuple[list[str], int, str]:
    stripped = document.lstrip("\ufeff \t\r\n")
    if not stripped:
        return [], 0, "empty"
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if not isinstance(data, dict) or not isinstance(data.get("outbounds"), list):
            raise ValueError("unsupported JSON subscription structure")
        outbounds = data["outbounds"][:MAX_NODES + 1]
        records = []
        rejected = 0
        for item in outbounds:
            try:
                records.append(_simple_structured(item, singbox=True))
            except (ValueError, TypeError):
                rejected += 1
        return records, rejected, "sing-box JSON (simple share-compatible records only)"
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if any(_SCHEME.match(line) for line in lines):
        return lines, 0, "URI lines"
    if depth < 2 and lines and all(not _SCHEME.match(line) for line in lines):
        try:
            decoded = _decode64(stripped).decode("utf-8-sig")
        except (ValueError, UnicodeError):
            pass
        else:
            if decoded != stripped and len(decoded.encode()) <= MAX_DOCUMENT:
                inner, rejected, fmt = _records(decoded, depth + 1)
                return inner, rejected, "Base64 of " + fmt
    if ":" in stripped:
        data = yaml.safe_load(stripped)
        if isinstance(data, dict) and isinstance(data.get("proxies"), list):
            records = []
            rejected = 0
            for item in data["proxies"][:MAX_NODES + 1]:
                try:
                    records.append(_simple_structured(item))
                except (ValueError, TypeError):
                    rejected += 1
            return records, rejected, "Clash YAML (simple share-compatible records only)"
    return lines, 0, "URI lines"


def parse_subscription(payload: bytes) -> Parsed:
    if not payload or len(payload) > MAX_DOCUMENT:
        raise ValueError("subscription document must be 1–4 MB")
    try:
        records, structured_rejected, fmt = _records(payload.decode("utf-8-sig"))
    except (UnicodeError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ValueError("subscription document is malformed") from exc
    if len(records) > MAX_NODES:
        raise ValueError("subscription exceeds 10,000 records")
    nodes: dict[str, Node] = {}
    unsupported = structured_rejected
    errors = 0
    for value in records:
        try:
            node = _node(value)
        except ValueError as exc:
            if "unsupported" in str(exc) or "unmapped" in str(exc):
                unsupported += 1
            else:
                errors += 1
            continue
        nodes.setdefault(node.identity, node)
    parsed = len(records) - (unsupported - structured_rejected) - errors
    return Parsed(tuple(nodes.values()), len(records) + structured_rejected, parsed,
                  parsed - len(nodes), unsupported, errors, fmt)


def public_name(number: int) -> str:
    if number < 1:
        raise ValueError("invalid MikaSub node number")
    return NAME_PREFIX + f"{number:04d}"


def validate_share_uri(value: str) -> bool:
    try:
        _node(value)
        return True
    except ValueError:
        return False
