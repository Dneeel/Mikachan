"""MikaSub's lossless boundary, limits, and offline export contract."""

import base64
import json
from urllib.parse import unquote

import pytest

from mikachan.mikasub_codec import parse_subscription, public_name, rename
from mikachan.mikasub_fetch import PublicResolver, source_url
from mikachan.mikasub_viewer import offline_viewer


def _b64(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_mixed_protocols_preserve_connection_bytes_and_exact_public_name():
    records = [
        "vless://uuid@one.example:443?security=reality&sni=long.example&fp=chrome#Old",
        "trojan://password@two.example:443?security=tls&type=ws&path=%2Fapi#T",
        "ss://YWVzLTEyOC1nY206dGVzdA@three.example:8388/?plugin=obfs-local%3Bobfs%3Dhttp#S",
        "hysteria2://password@four.example:443?insecure=0&sni=other.example#H",
        "tuic://uuid:password@five.example:443?alpn=h3#U",
        "ssr://" + _b64("six.example:8388:auth_aes128_md5:aes-256-cfb:plain:" +
                         _b64("secret") + "/?obfsparam=" + _b64("host.example") +
                         "&remarks=" + _b64("SSR Old")),
        "vmess://" + _b64(json.dumps({"add": "seven.example", "id": "uuid", "ps": "VMess Old",
                                      "net": "ws", "path": "/x?y=z", "tls": "tls", "sni": "s.example"})),
    ]
    result = parse_subscription(("\n".join(records) + "\n").encode())
    assert result.parsed == result.raw == len(records)
    assert {node.protocol for node in result.nodes} == {
        "vless", "trojan", "ss", "hysteria2", "tuic", "ssr", "vmess"}
    for index, node in enumerate(result.nodes, 1):
        updated = rename(node, public_name(index))
        assert parse_subscription(updated.encode()).nodes[0].identity == node.identity
        assert public_name(index) in (unquote(updated.rsplit("#", 1)[-1]) if node.protocol not in
                                      {"ssr", "vmess"} else public_name(index))
    assert public_name(10000).endswith("10000")
    assert result.nodes[-1].name == "VMess Old"


def test_base64_bom_duplicates_and_unsupported_are_counted_honestly():
    raw = ("vless://u@host.example:443?security=tls#one\n"
           "vless://u@host.example:443?security=tls#two\n"
           "unknown://opaque@host.example:443#not-safe-to-rename\n"
           "vmess://broken!!!\n")
    encoded = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    wrapped = "\ufeff" + encoded[:64] + "\n" + encoded[64:]
    result = parse_subscription(wrapped.encode())
    assert result.raw == 4 and result.parsed == 2
    assert result.duplicates == 1 and result.unsupported == 1 and result.errors == 1
    assert len(result.nodes) == 1
    assert result.source_format.startswith("Base64")


def test_simple_structured_formats_and_unmapped_options():
    clash = b"""# Clash subscription
mode: rule
proxies:
  - name: Safe
    type: ss
    server: ss.example
    port: 8388
    cipher: aes-256-gcm
    password: secret
  - name: Needs native export
    type: vless
    server: v.example
    port: 443
    uuid: abc
    reality-opts: {public-key: abc}
"""
    result = parse_subscription(clash)
    assert result.raw == 2 and result.parsed == 1 and result.unsupported == 1
    assert result.nodes[0].protocol == "ss"
    singbox = {"outbounds": [{"type": "trojan", "tag": "Simple", "server": "t.example",
                               "server_port": 443, "password": "abc", "sni": "s.example"},
                              {"type": "vless", "server": "v.example", "server_port": 443,
                               "uuid": "abc", "transport": {"type": "ws"}}]}
    parsed = parse_subscription(json.dumps(singbox).encode())
    assert parsed.parsed == 1 and parsed.unsupported == 1
    assert "sni=s.example" in parsed.nodes[0].uri


def test_large_mixed_collection_and_limits():
    data = "\n".join(f"vless://u{i}@host{i}.example:443?security=tls#Name {i}"
                     for i in range(4001)).encode()
    parsed = parse_subscription(data)
    assert len(parsed.nodes) == 4001 and parsed.duplicates == 0
    with pytest.raises(ValueError, match="5,000"):
        parse_subscription("\n".join("trojan://u@h.example:443#x" for _ in range(5001)).encode())


def test_html_is_offline_injection_safe_and_qr_is_on_demand():
    malicious = "vless://u@host.example:443#</script><img src=x onerror=alert(1)>"
    view = offline_viewer([{"uri": malicious, "name": "<b>Danger</b>",
                            "protocol": "vless"}], title="MikaSub")
    text = view.decode()
    assert "const items=" in text
    assert "\\u003c/script\\u003e" in text
    assert "<img src=x" not in text
    assert "Copy All" in text and "clipboard.writeText" in text
    assert "Manual copy" in text and "qrcode(0,'L')" in text
    assert "https://cdn" not in text and "fetch(" not in text
    assert len(view) < 100_000  # The QR library is included once, not one image per node.


@pytest.mark.parametrize("value", [
    "http://127.0.0.1/admin", "http://[::1]/", "http://10.0.0.1/test",
    "http://169.254.169.254/latest/meta-data", "http://localhost/test",
    "https://user:pass@public.example/sub", "https://public.example:8080/sub",
    "https://public.example/sub#frag", "https://public.example/%0a\nsecret",
])
def test_source_url_rejects_internal_or_unsafe_targets(value):
    with pytest.raises(ValueError):
        source_url(value)


@pytest.mark.asyncio
async def test_resolver_rejects_rebound_private_dns(monkeypatch):
    class Loop:
        async def getaddrinfo(self, *_args, **_kwargs):
            return [(2, 1, 6, "", ("203.0.113.4", 443)),
                    (2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr("mikachan.mikasub_fetch.asyncio.get_running_loop", lambda: Loop())
    with pytest.raises(ValueError, match="private address"):
        await PublicResolver().resolve("example.com", 443)
