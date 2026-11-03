"""受限的纯 XML RSS/Atom 解析器。

解析流程参考 MoviePilot RSS 结果标准化边界，参考
`MoviePilot@04963c46080c2496f2df3fe5958f6a4eb75a3910`，GPL-3.0。
实现独立编写，不读取规则包，不发起 HTTP 请求，也不依据发布时间推进游标。
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from lxml import etree
from pydantic import SecretStr

from pt_mcp.adapters.base import (
    AdapterError,
    ResourceRecord,
    SiteConfig,
    category_values,
    is_sensitive_query_key,
    make_resource_id,
    public_detail_url,
)

MAX_RSS_BYTES = 2 * 1024 * 1024
MAX_RSS_ITEMS = 500


def parse_rss(
    content: bytes | str,
    *,
    feed_url: str,
    site: SiteConfig,
    max_items: int = MAX_RSS_ITEMS,
    max_bytes: int = MAX_RSS_BYTES,
) -> list[ResourceRecord]:
    """从 RSS 2.0、RDF/RSS 1.0 或 Atom XML 提取资源记录。

    DTD、实体声明和网络实体解析全部禁用。单次解析最多接受 2 MiB XML，
    并返回至多 500 项；这些上限可由调用方收紧，不能扩大。
    """
    if max_items < 1 or max_bytes < 1:
        raise ValueError("RSS 限额必须是正数")
    byte_limit = min(max_bytes, MAX_RSS_BYTES)
    item_limit = min(max_items, MAX_RSS_ITEMS)
    if isinstance(content, str):
        raw = content.encode("utf-8")
    else:
        raw = content
    if len(raw) > byte_limit:
        raise AdapterError("RSS feed 超过大小限制", code="rss_too_large")

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
        remove_comments=True,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as error:
        raise AdapterError("RSS feed 不是有效 XML", code="rss_invalid") from error
    tree = root.getroottree()
    if tree.docinfo.doctype or tree.docinfo.internalDTD or tree.docinfo.externalDTD:
        raise AdapterError("RSS feed 不允许声明 DTD 或实体", code="rss_unsafe_xml")

    items = _feed_items(root)
    return [
        record
        for item in items[:item_limit]
        if (record := _parse_item(item, feed_url=feed_url, site=site)) is not None
    ]


def _feed_items(root: etree._Element) -> list[etree._Element]:
    """从 RSS、RDF/RSS 或 Atom 根节点定位 item/entry。"""
    root_name = _local_name(root)
    if root_name == "rss":
        return [node for node in root.iter() if _local_name(node) == "item"]
    if root_name == "rdf":
        return [node for node in root.iter() if _local_name(node) == "item"]
    if root_name == "feed":
        return [node for node in root if _local_name(node) == "entry"]
    raise AdapterError("RSS feed 根节点不是 rss、rdf 或 Atom feed", code="rss_invalid")


def _parse_item(item: etree._Element, *, feed_url: str, site: SiteConfig) -> ResourceRecord | None:
    """提取一个条目，缺失的可选数据保持 None。"""
    title = _child_text(item, "title")
    if not title:
        return None

    link = _item_link(item, "alternate") or _item_link(item, None)
    enclosure = _enclosure_url(item)
    guid = _child_text(item, "guid") or _child_text(item, "id")
    source_identity = _stable_source_identity(guid, link, enclosure, feed_url)
    if not source_identity:
        return None

    public_id = make_resource_id(site.site_id, source_identity)
    categories = category_values(site, _categories(item))
    size = _optional_int(_enclosure_length(item))
    seeders = _optional_int(_child_text(item, "seeders"))
    peers = _optional_int(_child_text(item, "leechers") or _child_text(item, "peers"))
    published = _parse_datetime(
        _child_text(item, "pubDate")
        or _child_text(item, "published")
        or _child_text(item, "updated")
    )

    locator = urljoin(feed_url, enclosure) if enclosure else None
    if not locator and link and link.lower().startswith("magnet:"):
        locator = link
    detail_url = public_detail_url(link, base_url=feed_url)
    return ResourceRecord(
        site_id=site.site_id,
        resource_id=public_id,
        source_id=source_identity,
        native_categories=categories,
        title=title,
        description=_child_text(item, "description") or _child_text(item, "summary") or None,
        detail_url=detail_url,
        size_bytes=size,
        seeders=seeders,
        peers=peers,
        published_at=published,
        labels=[],
        locator=SecretStr(locator) if locator else None,
        config_version=site.config_version,
    )


def _local_name(node: etree._Element) -> str:
    """读取命名空间无关的本地元素名。"""
    return etree.QName(node).localname.lower()


def _children(node: etree._Element, name: str) -> Iterable[etree._Element]:
    """枚举本地名称匹配的直接子元素。"""
    expected = name.lower()
    return (child for child in node if _local_name(child) == expected)


def _child_text(node: etree._Element, name: str) -> str | None:
    """取得首个子元素的文本或 Atom content 摘要。"""
    child = next(iter(_children(node, name)), None)
    if child is None:
        return None
    text = " ".join("".join(child.itertext()).split())
    return text or None


def _item_link(item: etree._Element, relation: str | None) -> str | None:
    """兼容 RSS 的 link 文本与 Atom 的 href 属性。"""
    for child in _children(item, "link"):
        href = child.get("href")
        if href:
            child_relation = child.get("rel", "alternate").lower()
            if relation is None or child_relation == relation:
                return href.strip()
        if relation is None and child.text and child.text.strip():
            return child.text.strip()
    return None


def _enclosure_url(item: etree._Element) -> str | None:
    """读取 RSS enclosure 或 Atom enclosure link。"""
    for child in _children(item, "enclosure"):
        url = child.get("url")
        if url:
            return url.strip()
    for child in _children(item, "link"):
        if child.get("rel", "").lower() == "enclosure" and child.get("href"):
            return child.get("href").strip()
    return None


def _enclosure_length(item: etree._Element) -> str | None:
    """读取 RSS enclosure length 或 Atom enclosure length。"""
    for child in (*_children(item, "enclosure"), *_children(item, "link")):
        if child.get("length") is not None and (
            _local_name(child) == "enclosure" or child.get("rel", "").lower() == "enclosure"
        ):
            return child.get("length")
    return None


def _categories(item: etree._Element) -> list[str]:
    """保留条目声明的所有原生 category/id 值和出现顺序。"""
    values: list[str] = []
    for child in _children(item, "category"):
        value = child.get("term") or child.text
        if value and value.strip():
            values.append(value.strip())
    return values


def _stable_source_identity(
    guid: str | None,
    link: str | None,
    enclosure: str | None,
    feed_url: str,
) -> str | None:
    """Prefer GUID, then stable detail link, and finally the enclosure URL.

    Known credential and expiring-token parameters are omitted from URL identity
    while the complete enclosure remains in the private locator field.
    """
    value = guid or link or enclosure
    if not value:
        return None
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "magnet"} and not value.startswith("/") and "?" not in value and "/" not in value:
        return value

    absolute = urljoin(feed_url, value) if not parsed.scheme else value
    parsed = urlsplit(absolute)
    if parsed.scheme == "magnet":
        safe_query = urlencode(sorted(
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not is_sensitive_query_key(key)
        ))
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, safe_query, ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return value

    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return value
    if port and not (parsed.scheme == "https" and port == 443) and not (parsed.scheme == "http" and port == 80):
        host = f"{host}:{port}"
    safe_query = urlencode(sorted(
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not is_sensitive_query_key(key)
    ))
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", safe_query, ""))


def _optional_int(value: str | None) -> int | None:
    """解析可缺省的整数，不把无效或空值伪装成零。"""
    if value is None:
        return None
    try:
        return int(value.strip())
    except (AttributeError, TypeError, ValueError):
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    """解析 RSS 日期或 ISO 日期；缺失、无效时保持未知。"""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = ["MAX_RSS_BYTES", "MAX_RSS_ITEMS", "parse_rss"]
