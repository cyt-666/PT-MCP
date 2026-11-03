"""Service integration tests use local SQLite and synthetic HTTP/downloader ports."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr
import pytest

from pt_mcp.config import AppConfig
from pt_mcp.credentials import SiteCredentials
from pt_mcp.errors import IdempotencyConflict, SubmissionTransportUnknown
from pt_mcp.models import (
    DownloadAck,
    DownloadView,
    DownloaderConfig,
    NativeCategory,
    ResourceRecord,
    SearchPage,
    SiteConfig,
    make_resource_id,
)
from pt_mcp.service import PtMcpService
from pt_mcp.storage import SQLiteStorage
from pt_mcp.identity import download_hashes


def _bencode(value: object) -> bytes:
    """Encode the small deterministic torrent structures used by these tests."""
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, dict):
        pairs = sorted(value.items())
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in pairs) + b"e"
    raise TypeError(type(value).__name__)


def _torrent_bytes() -> bytes:
    info = {b"name": b"synthetic-resource", b"piece length": 16384, b"pieces": b"x" * 20}
    return _bencode({b"announce": b"https://tracker.invalid/announce", b"info": info})


def _site(*, rss_url_env: str | None = None) -> SiteConfig:
    return SiteConfig(
        site_id="site-a",
        name="Synthetic site",
        kind="nexusphp",
        base_url="https://tracker.example",
        credential_ref="site-a",
        rss_url_env=rss_url_env,
        native_categories=[NativeCategory(id="ebooks", label="E-books")],
    )


class _Credentials:
    """Return only synthetic credentials for tests."""

    def __init__(self, *, rss_url: str | None = None) -> None:
        self.rss_url = rss_url

    async def get(self, reference: str | None) -> SiteCredentials | None:
        if reference is None:
            return None
        return SiteCredentials(
            cookie=SecretStr("synthetic-cookie"),
            rss_url=SecretStr(self.rss_url) if self.rss_url else None,
        )


class _Http:
    """Return queued in-memory feed responses and record request metadata."""

    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def request(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if not self.bodies:
            raise AssertionError("unexpected HTTP request")
        body = self.bodies.pop(0)
        return SimpleNamespace(
            status_code=200,
            headers={},
            body=body,
            text=body.decode("utf-8", errors="replace"),
            url="https://tracker.example/rss.xml",
        )

    async def aclose(self) -> None:
        self.closed = True


class _Adapter:
    """Return synthetic resource records and torrent bytes."""

    def __init__(self, records: list[ResourceRecord], content: bytes) -> None:
        self.records = records
        self.content = content
        self.resolved: list[str] = []
        self.search_calls: list[dict[str, Any]] = []

    async def search_torrents(
        self,
        site: SiteConfig,
        *,
        keyword: str,
        category_ids: list[str] | None,
        page: int,
    ) -> SearchPage[ResourceRecord]:
        self.search_calls.append(
            {"site_id": site.site_id, "keyword": keyword, "category_ids": category_ids, "page": page}
        )
        return SearchPage(items=self.records, page=page, has_more=False)

    async def resolve_download(self, site: SiteConfig, item: ResourceRecord) -> bytes:
        self.resolved.append(item.source_id)
        return self.content


class _Downloader:
    """Fake qBittorrent port with controllable add acknowledgement and hash reads."""

    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.add_calls: list[dict[str, Any]] = []
        self.tasks: list[DownloadView] = []

    async def add(self, *, content: bytes | str, save_path: str | None, paused: bool = False) -> DownloadAck:
        self.add_calls.append({"content": content, "save_path": save_path, "paused": paused})
        if self.unknown:
            raise SubmissionTransportUnknown()
        self.tasks.append(
            DownloadView(
                hashes=sorted(download_hashes(content)),
                name="synthetic resource",
                state="downloading",
            )
        )
        return DownloadAck(accepted=True)

    async def get_by_hashes(self, hashes: set[str]) -> list[DownloadView]:
        return [task for task in self.tasks if set(task.hashes).intersection(hashes)]

    async def list_downloads(self, *, hashes: set[str] | None = None) -> list[DownloadView]:
        return [
            task for task in self.tasks
            if hashes is None or set(task.hashes).intersection(hashes)
        ]

    async def aclose(self) -> None:
        return None


def _record(source_id: str, content: bytes) -> ResourceRecord:
    return ResourceRecord(
        site_id="site-a",
        resource_id=make_resource_id("site-a", source_id),
        source_id=source_id,
        public_source_id=source_id,
        native_categories=[NativeCategory(id="ebooks", label="E-books")],
        title="Synthetic resource",
        locator=SecretStr("https://tracker.example/download?id=synthetic"),
    )


def _service(
    tmp_path: Path,
    *,
    records: list[ResourceRecord] | None = None,
    adapter: Any | None = None,
    downloader: Any | None = None,
    http: Any | None = None,
    credentials: Any | None = None,
    rss_url_env: str | None = None,
) -> PtMcpService:
    site = _site(rss_url_env=rss_url_env)
    downloaders = (
        [DownloaderConfig(
            downloader_id="default",
            kind="qbittorrent",
            host="https://qbit.invalid",
            credential_ref="qbit",
            save_profiles={"library": "/downloads/library"},
            default_save_profile_id="library",
        )]
        if downloader is not None else []
    )
    config = AppConfig(
        data_dir=tmp_path / "state",
        sites=[site],
        downloaders=downloaders,
    )
    active_adapter = adapter or _Adapter(records or [], _torrent_bytes())
    active_downloader = downloader or _Downloader()
    return PtMcpService(
        config,
        storage=SQLiteStorage(config.data_dir),
        http=http or _Http(),
        credential_provider=credentials or _Credentials(),
        adapters={"site-a": active_adapter},
        downloaders={"default": active_downloader} if downloaders else {},
    )


def test_empty_configuration_lists_capabilities_without_network_or_credentials(tmp_path: Path) -> None:
    """The MCP service can discover its fixed tools with no sites or downloaders."""
    config = AppConfig(data_dir=tmp_path / "empty-state", sites=[], downloaders=[])
    service = PtMcpService(config, storage=SQLiteStorage(config.data_dir), http=_Http())

    async def run() -> None:
        assert await service.list_sites() == []
        assert await service.list_downloaders() == []
        await service.aclose()

    asyncio.run(run())


def test_search_caches_private_locator_but_returns_native_id_only(tmp_path: Path) -> None:
    """Search emits a stable opaque ID without leaking the private locator."""
    record = _record("native-101", _torrent_bytes())
    adapter = _Adapter([record], _torrent_bytes())
    service = _service(tmp_path, records=[record], adapter=adapter)

    async def run() -> None:
        page = await service.search_torrents("site-a", "book title", ["ebooks"], 1)
        assert page.page == 1 and page.has_more is False
        item = page.items[0]
        assert item.resource_id == make_resource_id("site-a", "native-101")
        assert item.source_id == "native-101"
        assert "locator" not in item.model_dump_json().lower()
        cached = await service.storage.get_resource(item.resource_id)
        assert cached is not None
        assert cached.locator is not None
        assert cached.locator.get_secret_value().endswith("id=synthetic")
        await service.aclose()

    asyncio.run(run())


def test_add_download_posts_once_and_replays_before_resource_lookup(tmp_path: Path, monkeypatch) -> None:
    """Repeated add calls with the same ID return the first accepted receipt."""
    content = _torrent_bytes()
    record = _record("native-201", content)
    adapter = _Adapter([record], content)
    downloader = _Downloader()
    service = _service(tmp_path, records=[record], adapter=adapter, downloader=downloader)

    async def run() -> None:
        await service.storage.put_resources([service._normalize_record(_site(), record)])
        first = await service.add_download(
            record.resource_id,
            "request-stable-1",
            save_profile_id="library",
        )
        replay = await service.add_download(
            record.resource_id,
            "request-stable-1",
            save_profile_id="library",
        )
        assert first.state == replay.state == "accepted"
        assert first.request_id == replay.request_id == "request-stable-1"
        assert len(downloader.add_calls) == 1
        assert downloader.add_calls[0]["save_path"] == "/downloads/library"

        old_site_version = service._site_version(_site())
        assert await service.storage.get_resource(
            record.resource_id,
            now=datetime.now(timezone.utc) + timedelta(days=2),
        ) is None
        changed_site = _site().model_copy(update={"config_version": "2"})
        service.config = service.config.model_copy(update={"sites": [changed_site]})
        service._sites = {changed_site.site_id: changed_site}
        assert service._site_version(changed_site) != old_site_version

        async def expired_resource(_resource_id: str, **_kwargs: Any) -> None:
            pytest.fail("an existing request must replay without resolving an expired or invalidated resource")

        monkeypatch.setattr(service.storage, "get_resource", expired_resource)
        replay = await service.add_download(
            record.resource_id,
            "request-stable-1",
            save_profile_id="library",
        )
        assert replay.state == "accepted"
        with pytest.raises(IdempotencyConflict):
            await service.add_download(
                record.resource_id,
                "request-stable-1",
                save_profile_id="library",
                paused=True,
            )
        assert len(downloader.add_calls) == 1
        await service.aclose()

    asyncio.run(run())


def test_unknown_add_is_reconciled_by_hash_and_never_reposted(tmp_path: Path) -> None:
    """A transport-unknown result stays unknown when hash lookup finds no task."""
    content = _torrent_bytes()
    record = _record("native-202", content)
    downloader = _Downloader(unknown=True)
    adapter = _Adapter([record], content)
    service = _service(tmp_path, records=[record], adapter=adapter, downloader=downloader)

    async def run() -> None:
        await service.storage.put_resources([service._normalize_record(_site(), record)])
        first = await service.add_download(record.resource_id, "request-unknown-1")
        reconciled = await service.get_submission("request-unknown-1")
        assert first.state == reconciled.state == "unknown"
        assert len(downloader.add_calls) == 1
        stored = await service.storage.get_request("request-unknown-1")
        assert stored is not None and stored.state == "unknown"

        old_site_version = service._site_version(_site())
        assert await service.storage.get_resource(
            record.resource_id,
            now=datetime.now(timezone.utc) + timedelta(days=2),
        ) is None
        changed_site = _site().model_copy(update={"config_version": "2"})
        service.config = service.config.model_copy(update={"sites": [changed_site]})
        service._sites = {changed_site.site_id: changed_site}
        assert service._site_version(changed_site) != old_site_version
        replay = await service.add_download(record.resource_id, "request-unknown-1")
        assert replay == stored
        with pytest.raises(IdempotencyConflict):
            await service.add_download(record.resource_id, "request-unknown-1", paused=True)
        assert adapter.resolved == [record.source_id]
        assert len(downloader.add_calls) == 1
        assert service.http.calls == []
        await service.aclose()

    asyncio.run(run())


def test_different_request_ids_cannot_double_post_same_torrent_hash(tmp_path: Path) -> None:
    """A retained unknown hash claim blocks a second request for the same torrent."""
    content = _torrent_bytes()
    first_record = _record("native-301", content)
    second_record = _record("native-302", content)
    downloader = _Downloader(unknown=True)
    service = _service(
        tmp_path,
        records=[first_record, second_record],
        adapter=_Adapter([first_record, second_record], content),
        downloader=downloader,
    )

    async def run() -> None:
        await service.storage.put_resources(
            [
                service._normalize_record(_site(), first_record),
                service._normalize_record(_site(), second_record),
            ]
        )
        first, second = await asyncio.gather(
            service.add_download(first_record.resource_id, "request-hash-1"),
            service.add_download(second_record.resource_id, "request-hash-2"),
        )
        assert {first.request_id, second.request_id} == {
            "request-hash-1", "request-hash-2",
        }
        assert {first.state, second.state} == {"unknown", "failed"}
        failed_request_id = first.request_id if first.state == "failed" else second.request_id
        failed_receipt = await service.storage.get_request(failed_request_id)
        assert failed_receipt is not None
        assert failed_receipt.request_id == failed_request_id
        assert failed_receipt.state == "failed"
        assert failed_receipt.error_code == "hash_in_flight"
        failed_resource_id = (
            first_record.resource_id if failed_request_id == "request-hash-1"
            else second_record.resource_id
        )
        replay = await service.add_download(failed_resource_id, failed_request_id)
        assert replay.request_id == failed_request_id
        assert replay.state == "failed"
        first_failed_check = await service.get_submission(failed_request_id)
        assert first_failed_check == failed_receipt
        owner_request_id = first.request_id if first.state == "unknown" else second.request_id
        await service.storage.finish_request(owner_request_id, state="accepted")
        after_owner_release = await service.get_submission(failed_request_id)
        after_owner_retry = await service.add_download(failed_resource_id, failed_request_id)
        assert after_owner_release == after_owner_retry == failed_receipt
        assert len(downloader.add_calls) == 1
        await service.aclose()

    asyncio.run(run())


def test_restarted_service_lazily_reconciles_unknown_receipt_by_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A new service instance builds its downloader lazily and never re-adds."""
    content = _torrent_bytes()
    record = _record("native-restart", content)
    original_downloader = _Downloader(unknown=True)
    service = _service(
        tmp_path,
        records=[record],
        adapter=_Adapter([record], content),
        downloader=original_downloader,
    )

    class ExistingTaskDownloader(_Downloader):
        """Represent the qBittorrent task accepted before the lost response."""

        def __init__(self) -> None:
            super().__init__()
            self.tasks.append(
                DownloadView(
                    hashes=sorted(download_hashes(content)),
                    name="synthetic resource",
                    state="downloading",
                )
            )

    reconciler = ExistingTaskDownloader()

    async def run() -> None:
        await service.storage.put_resources([service._normalize_record(_site(), record)])
        unknown = await service.add_download(record.resource_id, "request-restarted-1")
        assert unknown.state == "unknown"
        await service.aclose()

        config = AppConfig(
            data_dir=tmp_path / "state",
            sites=[_site()],
            downloaders=[
                DownloaderConfig(
                    downloader_id="default",
                    kind="qbittorrent",
                    host="https://qbit.invalid",
                    credential_ref="qbit",
                    save_profiles={"library": "/downloads/library"},
                )
            ],
        )
        monkeypatch.setattr(
            "pt_mcp.service.QBittorrentDownloader",
            lambda downloader_config, credential_provider: reconciler,
        )
        restarted = PtMcpService(
            config,
            storage=SQLiteStorage(config.data_dir),
            http=_Http(),
            credential_provider=_Credentials(),
            adapters={"site-a": _Adapter([record], content)},
            downloaders={},
        )
        receipt = await restarted.get_submission("request-restarted-1")
        assert receipt is not None
        assert receipt.state == "accepted"
        assert receipt.task is not None
        assert set(receipt.task.hashes) == download_hashes(content)
        assert reconciler.add_calls == []
        await restarted.aclose()

    asyncio.run(run())


def test_cold_service_keeps_unknown_when_factory_lookup_finds_no_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A cold service lazily builds a downloader and never reposts an unknown receipt."""
    content = _torrent_bytes()
    record = _record("native-cold-miss", content)
    initial_downloader = _Downloader(unknown=True)
    original = _service(
        tmp_path,
        records=[record],
        adapter=_Adapter([record], content),
        downloader=initial_downloader,
    )
    cold_downloader = _Downloader()

    async def run() -> None:
        await original.storage.put_resources([original._normalize_record(_site(), record)])
        unknown = await original.add_download(record.resource_id, "request-cold-miss")
        assert unknown.state == "unknown"
        await original.aclose()

        config = AppConfig(
            data_dir=tmp_path / "state",
            sites=[_site()],
            downloaders=[
                DownloaderConfig(
                    downloader_id="default",
                    kind="qbittorrent",
                    host="https://qbit.invalid",
                    credential_ref="qbit",
                )
            ],
        )
        monkeypatch.setattr(
            "pt_mcp.service.QBittorrentDownloader",
            lambda downloader_config, credential_provider: cold_downloader,
        )
        cold_service = PtMcpService(
            config,
            storage=SQLiteStorage(config.data_dir),
            http=_Http(),
            credential_provider=_Credentials(),
            adapters={"site-a": _Adapter([record], content)},
            downloaders={},
        )
        assert cold_service._downloaders == {}
        first_check = await cold_service.get_submission("request-cold-miss")
        assert first_check is not None
        assert first_check.state == "unknown"
        assert "default" in cold_service._downloaders
        replay = await cold_service.add_download(record.resource_id, "request-cold-miss")
        assert replay == first_check
        assert cold_downloader.add_calls == []
        assert initial_downloader.add_calls and len(initial_downloader.add_calls) == 1
        await cold_service.aclose()

    asyncio.run(run())


def test_rss_incremental_cursor_survives_reordering_and_is_reader_scoped(tmp_path: Path) -> None:
    """Drained snapshots fetch on demand, ignore reordered seen IDs, and preserve readers."""
    initial = b"""<rss version="2.0"><channel>
      <item><guid>rss-1</guid><title>First</title><link>https://tracker.example/item/1</link>
        <enclosure url="https://tracker.example/file/1" length="10"/></item>
      <item><guid>rss-2</guid><title>Second</title><link>https://tracker.example/item/2</link>
        <enclosure url="https://tracker.example/file/2" length="20"/></item>
    </channel></rss>"""
    updated = b"""<rss version="2.0"><channel>
      <item><guid>rss-2</guid><title>Second</title><link>https://tracker.example/item/2</link>
        <enclosure url="https://tracker.example/file/2" length="20"/></item>
      <item><guid>rss-1</guid><title>First</title><link>https://tracker.example/item/1</link>
        <enclosure url="https://tracker.example/file/1" length="10"/></item>
      <item><guid>rss-0</guid><title>Older but new identity</title><link>https://tracker.example/item/0</link>
        <enclosure url="https://tracker.example/file/0" length="30"/></item>
    </channel></rss>"""
    http = _Http(initial, updated)
    service = _service(
        tmp_path,
        http=http,
        credentials=_Credentials(rss_url="https://tracker.example/rss.xml?passkey=synthetic"),
    )

    async def run() -> None:
        first = await service.read_rss("site-a", limit=1)
        assert len(first.items) == 1
        assert first.has_more is True

        second = await service.read_rss("site-a", cursor=first.cursor, limit=1)
        assert [item.title for item in second.items] == ["Second"]
        assert second.items[0].source_id is None
        assert second.has_more is False

        # Replaying an earlier immutable cursor repeats its page and does not move
        # another caller's cursor or consume its pending item.
        replay_second = await service.read_rss("site-a", cursor=first.cursor, limit=1)
        assert [item.title for item in replay_second.items] == ["Second"]
        assert replay_second.items[0].source_id is None
        assert replay_second.cursor == second.cursor

        incremental = await service.read_rss("site-a", cursor=second.cursor, limit=10)
        assert [item.title for item in incremental.items] == ["Older but new identity"]
        assert incremental.has_more is False
        assert "synthetic" not in incremental.items[0].model_dump_json()
        assert len(http.calls) == 2
        await service.aclose()

    asyncio.run(run())
