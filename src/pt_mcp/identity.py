"""BitTorrent 种子和磁力链接的稳定身份计算。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from urllib.parse import parse_qs, urlsplit


def _is_magnet_link(content: str | bytes) -> bool:
    """判断内容是否以 magnet scheme 开头。"""
    if isinstance(content, str):
        return content.startswith("magnet:")
    return isinstance(content, bytes) and content.startswith(b"magnet:")


def _bencoded_value_end(content: bytes, offset: int) -> int:
    """跳过一个 bencode 值，保留原始 info 字节以计算协议哈希。"""
    marker = content[offset : offset + 1]
    if marker == b"i":
        end = content.index(b"e", offset + 1)
        if not re.fullmatch(rb"0|-?[1-9][0-9]*", content[offset + 1 : end]):
            raise ValueError("invalid bencode integer")
        return end + 1
    if marker in (b"d", b"l"):
        offset += 1
        keys: set[bytes] = set()
        while content[offset : offset + 1] != b"e":
            if marker == b"d":
                if not content[offset : offset + 1].isdigit():
                    raise ValueError("invalid bencode dictionary key")
                separator = content.index(b":", offset)
                key_end = _bencoded_value_end(content, offset)
                key = content[separator + 1 : key_end]
                if key in keys:
                    raise ValueError("duplicate bencode dictionary key")
                keys.add(key)
                offset = key_end
            offset = _bencoded_value_end(content, offset)
        return offset + 1
    if marker.isdigit():
        separator = content.index(b":", offset)
        if not re.fullmatch(rb"0|[1-9][0-9]*", content[offset:separator]):
            raise ValueError("invalid bencode string length")
        end = separator + 1 + int(content[offset:separator])
        if end > len(content):
            raise ValueError("truncated bencode string")
        return end
    raise ValueError("invalid bencode value")


def _bencoded_dictionary(content: bytes) -> dict[bytes, bytes]:
    """返回 bencode 字典的直接键值原始切片。"""
    if not content.startswith(b"d") or _bencoded_value_end(content, 0) != len(content):
        raise ValueError("invalid bencode dictionary")
    result: dict[bytes, bytes] = {}
    offset = 1
    while content[offset : offset + 1] != b"e":
        if not content[offset : offset + 1].isdigit():
            raise ValueError("invalid bencode dictionary key")
        separator = content.index(b":", offset)
        key_end = _bencoded_value_end(content, offset)
        key = content[separator + 1 : key_end]
        value_end = _bencoded_value_end(content, key_end)
        if key in result:
            raise ValueError("duplicate bencode dictionary key")
        result[key] = content[key_end:value_end]
        offset = value_end
    return result


def download_hashes(content: str | bytes) -> frozenset[str]:
    """提取 torrent 或 magnet 中明确声明的 v1、v2 身份。

    v2 身份同时包含完整 SHA-256 和 qBittorrent 常见的 40 位截断值。
    解析失败返回空集合，不从名称、大小或 URL 推断身份。
    """
    try:
        hashes: set[str] = set()
        if _is_magnet_link(content):
            url = content.decode("utf-8") if isinstance(content, bytes) else content
            for value in parse_qs(urlsplit(url).query).get("xt", []):
                if match := re.fullmatch(r"urn:btih:([a-fA-F0-9]{40})", value):
                    hashes.add(match.group(1).lower())
                elif match := re.fullmatch(r"urn:btih:([a-zA-Z2-7]{32})", value):
                    hashes.add(base64.b32decode(match.group(1).upper()).hex())
                elif match := re.fullmatch(r"urn:btmh:1220([a-fA-F0-9]{64})", value):
                    digest = match.group(1).lower()
                    hashes.update((digest, digest[:40]))
        elif isinstance(content, bytes):
            info = _bencoded_dictionary(content).get(b"info", b"")
            fields = _bencoded_dictionary(info)
            if b"pieces" in fields:
                hashes.add(hashlib.sha1(info).hexdigest())
            if fields.get(b"meta version") == b"i2e":
                digest = hashlib.sha256(info).hexdigest()
                hashes.update((digest, digest[:40]))
        return frozenset(hashes)
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        return frozenset()


__all__ = ["download_hashes"]
