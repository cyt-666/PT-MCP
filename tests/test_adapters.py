"""NexusPHP and M-Team adapter tests use fake HTTP and synthetic fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from pydantic import SecretStr

from pt_mcp.adapters.mteam import MTeamAdapter, MTeamProfile
from pt_mcp.adapters.nexusphp import NexusPHPAdapter, NexusPHPProfile, load_profile_for_site
from pt_mcp.errors import AdapterError
from pt_mcp.models import NativeCategory, ResourceRecord, ResourceView, SiteConfig
from pt_mcp.credentials import SiteCredentials

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"
PROFILES = Path(__file__).parents[1] / "src" / "pt_mcp" / "resources" / "profiles"


class FakeCredentials:
    """Return only synthetic credentials from a local reference."""

    def __init__(self) -> None:
        self.lookups: list[str | None] = []

    async def get(self, reference: str | None) -> SiteCredentials | None:
        self.lookups.append(reference)
        if reference is None:
            return None
        return SiteCredentials(
            cookie=SecretStr("fixture-cookie"),
            api_key=SecretStr("fixture-api-key"),
        )


class FakeHttp:
    """Record adapter requests and return queued in-memory responses."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def _response(status: int, body: bytes | str = b"", *, headers: dict[str, str] | None = None) -> SimpleNamespace:
    """Build a minimal response object matching the core HTTP contract."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return SimpleNamespace(
        status_code=status,
        headers=headers or {},
        body=raw,
        text=raw.decode("utf-8", errors="replace"),
        url="https://tracker.example/",
    )


def _native_categories() -> list[NativeCategory]:
    """Return unrelated synthetic native categories without media buckets."""
    return [
        NativeCategory(id="ebooks", label="E-books"),
        NativeCategory(id="audio-books", label="Audio books"),
        NativeCategory(id="journals", label="Journals"),
    ]


def _nexus_site() -> SiteConfig:
    """Build a synthetic NexusPHP site with no real account credentials."""
    return SiteConfig(
        site_id="nexus-fixture",
        name="Synthetic NexusPHP",
        kind="nexusphp",
        base_url="https://tracker.example/",
        credential_ref="fixture-nexus",
        native_categories=_native_categories(),
        timeout_s=3,
    )


def _mteam_site(*, download_hosts: list[str] | None = None) -> SiteConfig:
    """Build a synthetic M-Team API site with no real account credentials."""
    return SiteConfig(
        site_id="mteam-fixture",
        name="Synthetic M-Team API",
        kind="mteam",
        base_url="https://tracker.example/",
        api_base_url="https://api.tracker.example/",
        credential_ref="fixture-mteam",
        native_categories=_native_categories(),
        download_hosts=download_hosts or [],
        timeout_s=3,
    )


def _load_profile(path: Path) -> dict[str, Any]:
    """Read an adapter-owned package profile for direct constructor injection."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_nexusphp_profile_loader_accepts_config_relative_and_absolute_paths(tmp_path: Path) -> None:
    """An administrator may select a local profile path in site configuration."""
    profile_path = tmp_path / "profiles" / "custom-nexus.yaml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text((PROFILES / "nexusphp.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    relative = load_profile_for_site("profiles/custom-nexus.yaml", profile_dir=tmp_path)
    absolute = load_profile_for_site(str(profile_path), profile_dir=tmp_path)

    assert relative.search.path == "/browse"
    assert absolute.search.item_xpath == relative.search.item_xpath


def test_nexusphp_search_uses_profile_fields_and_keeps_native_categories() -> None:
    """Nexus search passes explicit categories and projects each configured XPath field."""
    client = FakeHttp(_response(200, (FIXTURES / "nexusphp_page_1.html").read_bytes()))
    adapter = NexusPHPAdapter(
        client,
        FakeCredentials(),
        profile=NexusPHPProfile.from_mapping(_load_profile(PROFILES / "nexusphp.yaml")),
    )

    page = asyncio.run(
        adapter.search_torrents(
            _nexus_site(),
            keyword="moss",
            category_ids=["ebooks"],
            page=1,
        )
    )

    assert [record.source_id for record in page.items] == ["nx-resource-101", "nx-resource-102"]
    assert all(record.resource_id.startswith("res_") for record in page.items)
    assert ResourceView.from_record(page.items[0]).source_id == "nx-resource-101"
    assert [[category.id for category in record.native_categories] for record in page.items] == [
        ["ebooks"],
        ["audio-books"],
    ]
    assert page.items[0].seeders == 7
    assert page.items[0].peers == 2
    assert page.has_more is True
    assert client.calls[0]["params"] == {"query": "moss", "category": "ebooks", "page": "1"}
    assert client.calls[0]["cookies"] == "fixture-cookie"
    assert len(client.calls) == 1


def test_nexusphp_search_distinguishes_empty_auth_captcha_and_incompatible_pages() -> None:
    """Only an explicit empty marker becomes an empty result list."""
    profile = NexusPHPProfile.from_mapping(_load_profile(PROFILES / "nexusphp.yaml"))
    fixture_cases = [
        ("nexusphp_empty_page.html", None),
        ("nexusphp_login_page.html", "site_auth_required"),
        ("nexusphp_captcha_page.html", "captcha_required"),
    ]
    for fixture_name, expected_code in fixture_cases:
        adapter = NexusPHPAdapter(
            FakeHttp(_response(200, (FIXTURES / fixture_name).read_bytes())),
            FakeCredentials(),
            profile=profile,
        )
        if expected_code is None:
            page = asyncio.run(
                adapter.search_torrents(_nexus_site(), keyword="nothing", category_ids=None, page=1)
            )
            assert page.items == []
        else:
            try:
                asyncio.run(
                    adapter.search_torrents(_nexus_site(), keyword="nothing", category_ids=None, page=1)
                )
            except AdapterError as error:
                assert error.code == expected_code
            else:
                raise AssertionError(f"expected {expected_code}")

    incompatible = NexusPHPAdapter(
        FakeHttp(_response(200, "<html><body>unrecognized page</body></html>")),
        FakeCredentials(),
        profile=profile,
    )
    try:
        asyncio.run(
            incompatible.search_torrents(_nexus_site(), keyword="query", category_ids=None, page=1)
        )
    except AdapterError as error:
        assert error.code == "page_incompatible"
    else:
        raise AssertionError("incompatible HTML must not become an empty result page")


def test_nexusphp_download_is_deferred_until_explicit_resolve() -> None:
    """Search does not fetch torrent files; resolving a selected item does."""
    client = FakeHttp(
        _response(200, (FIXTURES / "nexusphp_page_1.html").read_bytes()),
        _response(200, b"synthetic torrent bytes"),
    )
    adapter = NexusPHPAdapter(
        client,
        FakeCredentials(),
        profile=NexusPHPProfile.from_mapping(_load_profile(PROFILES / "nexusphp.yaml")),
    )
    site = _nexus_site()

    page = asyncio.run(adapter.search_torrents(site, keyword="moss", category_ids=None, page=1))
    assert len(client.calls) == 1
    torrent_bytes = asyncio.run(adapter.resolve_download(site, page.items[0]))

    assert torrent_bytes == b"synthetic torrent bytes"
    assert len(client.calls) == 2
    assert client.calls[1]["url"] == "https://tracker.example/download.php?id=nx-resource-101"


def test_mteam_search_preserves_native_ids_and_does_not_resolve_download() -> None:
    """M-Team search uses the explicit API host and returns original IDs/categories."""
    client = FakeHttp(_response(200, (FIXTURES / "mteam_search_page.json").read_bytes()))
    adapter = MTeamAdapter(
        client,
        FakeCredentials(),
        profile=MTeamProfile.from_mapping(_load_profile(PROFILES / "mteam.yaml")),
    )

    page = asyncio.run(
        adapter.search_torrents(
            _mteam_site(),
            keyword="moss",
            category_ids=["ebooks", "audio-books"],
            page=1,
        )
    )

    assert [record.source_id for record in page.items] == ["mt-resource-301", "mt-resource-302"]
    assert all(record.resource_id.startswith("res_") for record in page.items)
    assert [[category.id for category in record.native_categories] for record in page.items] == [
        ["ebooks"],
        ["audio-books"],
    ]
    assert all(record.locator is None for record in page.items)
    assert ResourceView.from_record(page.items[0]).source_id == "mt-resource-301"
    assert page.has_more is True
    assert client.calls[0]["url"] == "https://api.tracker.example/api/torrent/search"
    assert client.calls[0]["json"]["categories"] == ["ebooks", "audio-books"]
    assert client.calls[0]["headers"]["x-api-key"] == "fixture-api-key"
    assert len(client.calls) == 1


def test_mteam_download_generates_token_only_on_resolve_and_keeps_api_key_private() -> None:
    """Token POST and binary GET happen only for explicit resolution; key is not forwarded."""
    client = FakeHttp(
        _response(
            200,
                json.dumps({"code": 0, "data": "https://cdn.tracker.example/file?token=short-lived-token-sentinel"}),
        ),
        _response(200, b"synthetic torrent bytes"),
    )
    adapter = MTeamAdapter(
        client,
        FakeCredentials(),
        profile=MTeamProfile.from_mapping(_load_profile(PROFILES / "mteam.yaml")),
    )
    site = _mteam_site(download_hosts=["cdn.tracker.example"])
    item = ResourceRecord(
        site_id=site.site_id,
        resource_id="res_fixture_301",
        source_id="mt-resource-301",
        native_categories=[NativeCategory(id="ebooks", label="E-books")],
        title="Synthetic item",
        locator=None,
    )

    data = asyncio.run(adapter.resolve_download(site, item))

    assert data == b"synthetic torrent bytes"
    assert len(client.calls) == 2
    assert client.calls[0]["url"] == "https://api.tracker.example/api/torrent/genDlToken"
    assert client.calls[0]["json"] == {"id": "mt-resource-301"}
    assert client.calls[0]["headers"]["x-api-key"] == "fixture-api-key"
    assert "headers" not in client.calls[1] or "x-api-key" not in client.calls[1]["headers"]
    assert "short-lived-token-sentinel" not in repr(data)


def test_mteam_download_redirect_to_unconfigured_host_is_blocked() -> None:
    """A token response cannot make the adapter fetch an undeclared host."""
    client = FakeHttp(
        _response(200, json.dumps({"code": 0, "data": "https://api.tracker.example/download"})),
        _response(302, headers={"Location": "https://outside.example/file"}),
    )
    adapter = MTeamAdapter(
        client,
        FakeCredentials(),
        profile=MTeamProfile.from_mapping(_load_profile(PROFILES / "mteam.yaml")),
    )
    site = _mteam_site()
    item = ResourceRecord(
        site_id=site.site_id,
        resource_id="res_fixture_301",
        source_id="mt-resource-301",
        native_categories=[],
        title="Synthetic item",
    )

    try:
        asyncio.run(adapter.resolve_download(site, item))
    except AdapterError as error:
        assert error.code == "download_host_blocked"
    else:
        raise AssertionError("redirect to unconfigured host must fail")
    assert len(client.calls) == 2
    assert all("outside.example" not in call["url"] for call in client.calls)
