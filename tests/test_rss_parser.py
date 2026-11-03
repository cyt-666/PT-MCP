"""RSS parser contract tests use only local synthetic feeds."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from pt_mcp.adapters.rss import MAX_RSS_BYTES, MAX_RSS_ITEMS, parse_rss
from pt_mcp.adapters.base import public_detail_url
from pt_mcp.errors import AdapterError
from pt_mcp.models import NativeCategory, ResourceView, SiteConfig

FIXTURES = Path(__file__).parent / "fixtures" / "rss"


def _site() -> SiteConfig:
    """Return a credential-free synthetic site configuration."""
    return SiteConfig(
        site_id="rss-fixture",
        name="Synthetic RSS site",
        kind="nexusphp",
        base_url="https://tracker.example/",
        native_categories=[
            NativeCategory(id="ebooks", label="E-books"),
            NativeCategory(id="open-license", label="Open license"),
            NativeCategory(id="archive", label="Archive"),
        ],
    )


def test_rss_parser_keeps_guid_categories_and_optional_facts() -> None:
    """GUID and every native category survive parsing; absent facts stay unknown."""
    content = (FIXTURES / "feed.xml").read_bytes()

    records = parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site())

    assert len(records) == 2
    first, second = records
    assert first.source_id == "feed-item-401"
    assert first.resource_id.startswith("res_")
    assert [(category.id, category.label) for category in first.native_categories] == [
        ("ebooks", "E-books"),
        ("open-license", "Open license"),
    ]
    assert first.size_bytes == 7340032
    assert first.seeders == 7
    assert first.peers == 2
    assert isinstance(first.locator, SecretStr)
    assert first.locator.get_secret_value() == "https://tracker.example/download?id=feed-item-401"
    assert second.source_id == "feed-item-402"
    assert second.native_categories[0].id == "archive"
    assert second.size_bytes == 0
    assert second.seeders is None
    assert second.peers is None
    assert second.published_at is None


def test_rss_parser_bounds_item_count_and_xml_size() -> None:
    """A caller can tighten but cannot enlarge the fixed parser limits."""
    content = (FIXTURES / "feed.xml").read_bytes()

    assert len(parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site(), max_items=1)) == 1
    with pytest.raises(AdapterError) as error:
        parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site(), max_bytes=8)
    assert error.value.code == "rss_too_large"
    assert MAX_RSS_BYTES == 2 * 1024 * 1024
    assert MAX_RSS_ITEMS == 500


def test_rss_parser_rejects_dtd_and_entity_declarations() -> None:
    """DTD/entity declarations are rejected without resolving their contents."""
    content = b'''<!DOCTYPE rss [<!ENTITY synthetic SYSTEM "file:///private/tmp/no-read">]>
    <rss version="2.0"><channel><item><title>&synthetic;</title></item></channel></rss>'''

    with pytest.raises(AdapterError) as error:
        parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site())

    assert error.value.code == "rss_unsafe_xml"


def test_rss_parser_handles_atom_and_hides_signed_urls() -> None:
    """Atom categories parse as native values and signed details are not public."""
    content = b'''<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Signed synthetic item</title>
        <id>https://tracker.example/item?id=one&amp;token=synthetic-secret</id>
        <link rel="alternate" href="https://tracker.example/detail?id=one&amp;passkey=synthetic-secret" />
        <link rel="enclosure" href="https://tracker.example/file?id=one&amp;token=synthetic-secret" length="9" />
        <category term="ebooks" />
      </entry>
    </feed>'''

    record = parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site())[0]

    assert record.resource_id.startswith("res_")
    assert "synthetic-secret" not in record.resource_id
    assert "synthetic-secret" not in record.source_id
    assert record.detail_url is None
    assert record.native_categories[0].id == "ebooks"
    assert record.size_bytes == 9
    assert record.locator is not None
    assert "synthetic-secret" in record.locator.get_secret_value()
    assert "synthetic-secret" not in repr(record)
    assert public_detail_url("/detail/token/synthetic-secret", base_url="https://tracker.example/") is None
    public = ResourceView.from_record(record)
    public_json = public.model_dump_json()
    assert "synthetic-secret" not in public_json
    assert public.source_id is None
    assert "locator" not in public.model_dump()


def test_rss_without_guid_prefers_stable_link_over_rotating_enclosure_token() -> None:
    """A signed enclosure can change without changing identity when its detail link is stable."""
    records = []
    for ephemeral in ("short-lived-a", "short-lived-b"):
        content = f'''<rss version="2.0"><channel><item>
          <title>Stable link item</title>
          <link>https://tracker.example/detail?id=stable-501</link>
          <enclosure url="https://tracker.example/download?id=stable-501&amp;token={ephemeral}" length="12" />
          <category>ebooks</category>
        </item></channel></rss>'''
        records.append(parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site())[0])

    assert records[0].source_id == "https://tracker.example/detail?id=stable-501"
    assert records[0].resource_id == records[1].resource_id
    assert records[0].locator != records[1].locator


def test_rss_link_auth_parameter_rotation_does_not_change_identity() -> None:
    """Authentication parameters on a fallback detail link do not enter stable identity."""
    records = []
    for passkey in ("synthetic-a", "synthetic-b"):
        content = f'''<rss version="2.0"><channel><item>
          <title>Rotating link credential</title>
          <link>https://tracker.example/detail?id=stable-502&amp;passkey={passkey}</link>
          <enclosure url="https://tracker.example/download?id=stable-502" length="12" />
        </item></channel></rss>'''
        records.append(parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site())[0])

    assert records[0].source_id == records[1].source_id
    assert records[0].resource_id == records[1].resource_id
    assert records[0].detail_url is None


def test_rss_enclosure_is_last_stable_identity_fallback() -> None:
    """When GUID and detail link are absent, rotating enclosure token values are stripped."""
    records = []
    for token in ("synthetic-a", "synthetic-b"):
        content = f'''<rss version="2.0"><channel><item>
          <title>Enclosure fallback item</title>
          <enclosure url="https://tracker.example/download?id=stable-503&amp;download_token={token}" length="12" />
        </item></channel></rss>'''
        records.append(parse_rss(content, feed_url="https://tracker.example/rss.xml", site=_site())[0])

    assert records[0].source_id == "https://tracker.example/download?id=stable-503"
    assert records[0].resource_id == records[1].resource_id
