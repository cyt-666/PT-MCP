"""离线验证 v1、v2、hybrid 种子与 magnet 身份。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from pt_mcp.identity import download_hashes


def _encode_bencode(value: object) -> bytes:
    """编码测试所需的最小 bencode 值。"""
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            _encode_bencode(key) + _encode_bencode(value[key])
            for key in sorted(value)
        ) + b"e"
    raise TypeError(f"unsupported fixture type: {type(value).__name__}")


def _torrent(*, v1: bool, v2: bool) -> tuple[bytes, bytes]:
    """生成可重复的 v1、v2 或 hybrid torrent fixture。"""
    info: dict[bytes, object] = {
        b"name": b"sample",
        b"piece length": 16384,
    }
    if v1:
        info[b"pieces"] = b"\xff" * 20
    if v2:
        info[b"meta version"] = 2
        info[b"file tree"] = {}
    raw_info = _encode_bencode(info)
    return _encode_bencode({b"announce": b"https://tracker.invalid/announce", b"info": info}), raw_info


@pytest.mark.parametrize(
    ("v1", "v2"),
    [(True, False), (False, True), (True, True)],
    ids=["v1", "v2", "hybrid"],
)
def test_torrent_hashes_use_original_info_bytes(v1: bool, v2: bool) -> None:
    """下载身份必须由原始 info 字节计算，v2 同时返回完整和截断形式。"""
    content, info = _torrent(v1=v1, v2=v2)

    expected: set[str] = set()
    if v1:
        expected.add(hashlib.sha1(info).hexdigest())
    if v2:
        digest = hashlib.sha256(info).hexdigest()
        expected.update((digest, digest[:40]))

    assert download_hashes(content) == frozenset(expected)


def test_magnet_hashes_accept_hex_base32_and_v2_multihash() -> None:
    """magnet 中的 v1 十六进制、Base32 和 v2 multihash 保留精确身份。"""
    v1 = "ab" * 20
    v2 = "cd" * 32
    base32 = base64.b32encode(bytes.fromhex(v1)).decode("ascii")

    assert download_hashes(f"magnet:?xt=urn:btih:{v1.upper()}") == frozenset({v1})
    assert download_hashes(f"magnet:?xt=urn:btih:{base32}") == frozenset({v1})
    assert download_hashes(
        f"magnet:?xt=urn:btih:{v1}&xt=urn:btmh:1220{v2}"
    ) == frozenset({v1, v2, v2[:40]})


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"torrent",
        b"d4:infod4:name4:Show6:pieces20:abce",
        b"d4:infodeeextra",
        b"d4:infod6:pieces0:6:lengthi+1eee",
        b"d4:infod6:pieces0:6:lengthi-0eee",
        "magnet:?xt=urn:btih:invalid",
        "magnet:?xt=urn:btmh:1220abcd",
        "https://tracker.invalid/file",
    ],
)
def test_invalid_or_unidentified_content_has_no_hash(content: str | bytes) -> None:
    """损坏内容、普通 URL 和缺少身份时不生成名称或大小替代身份。"""
    assert download_hashes(content) == frozenset()
