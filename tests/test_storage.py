"""Transactional storage tests use isolated temporary SQLite databases."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from pt_mcp.errors import IdempotencyConflict, NotFound
from pt_mcp.models import (
    DownloadView,
    NativeCategory,
    ResourceRecord,
    make_resource_id,
)
from pt_mcp.storage import SQLiteStorage


def _record(index: int, *, site_id: str = "site-a") -> ResourceRecord:
    """Build a complete private record with synthetic site identifiers."""
    source_id = f"native-{index}"
    return ResourceRecord(
        site_id=site_id,
        resource_id=make_resource_id(site_id, source_id),
        source_id=source_id,
        native_categories=[NativeCategory(id="books", label="Books")],
        title=f"Synthetic item {index}",
        size_bytes=index,
        locator=SecretStr(f"https://tracker.example/dl?id={index}&passkey=private-{index}"),
    )


@pytest.fixture()
def storage(tmp_path: Path):
    """Create a storage object in a fresh private directory and close it after use."""
    value = SQLiteStorage(tmp_path / "pt-mcp-state")
    try:
        yield value
    finally:
        asyncio.run(value.aclose())


def test_resource_cache_roundtrip_keeps_locator_private_and_checks_version_and_ttl(storage: SQLiteStorage) -> None:
    """Cached records restore SecretStr locators and expire on either guard."""
    now = datetime.now(timezone.utc)
    record = _record(1)

    asyncio.run(storage.put_resources([record], ttl_seconds=60))

    restored = asyncio.run(storage.get_resource(record.resource_id, config_version="1", now=now))
    assert restored is not None
    assert restored.source_id == "native-1"
    assert restored.locator is not None
    assert restored.locator.get_secret_value() == "https://tracker.example/dl?id=1&passkey=private-1"
    connection = sqlite3.connect(storage._database)
    try:
        payload, locator = connection.execute(
            "SELECT payload_json, private_locator FROM resources WHERE resource_id=?",
            (record.resource_id,),
        ).fetchone()
    finally:
        connection.close()
    assert "private-1" not in payload
    assert locator == "https://tracker.example/dl?id=1&passkey=private-1"
    assert asyncio.run(storage.get_resource(record.resource_id, config_version="different", now=now)) is None
    still_valid = asyncio.run(storage.get_resource(record.resource_id, config_version="1", now=now))
    assert still_valid is not None
    assert asyncio.run(storage.get_resource(record.resource_id, now=now)) is not None
    asyncio.run(storage.put_resources([record], ttl_seconds=60))
    assert asyncio.run(
        storage.get_resource(record.resource_id, config_version="1", now=now + timedelta(seconds=61))
    ) is None


def test_resource_locator_secret_survives_storage_reopen(tmp_path: Path) -> None:
    """A private locator survives SQLite close/reopen without SecretStr masking."""
    data_dir = tmp_path / "state"
    first = SQLiteStorage(data_dir)
    record = _record(7)
    try:
        asyncio.run(first.put_resources([record]))
        database = first._database
    finally:
        asyncio.run(first.aclose())

    reopened = SQLiteStorage(data_dir)
    try:
        restored = asyncio.run(reopened.get_resource(record.resource_id))
        assert restored is not None
        assert restored.locator is not None
        assert restored.locator.get_secret_value() == "https://tracker.example/dl?id=7&passkey=private-7"
        assert database.exists()
    finally:
        asyncio.run(reopened.aclose())


def test_storage_permissions_do_not_change_an_existing_directory(tmp_path: Path) -> None:
    """Only a newly-created app state directory is chmodded to 0700."""
    if os.name == "nt":
        pytest.skip("POSIX mode checks are not available on Windows")

    created = SQLiteStorage(tmp_path / "created" / "state")
    try:
        assert stat.S_IMODE((tmp_path / "created" / "state").stat().st_mode) == 0o700
        assert stat.S_IMODE(created._database.stat().st_mode) == 0o600
        wal = Path(f"{created._database}-wal")
        if wal.exists():
            assert stat.S_IMODE(wal.stat().st_mode) == 0o600
    finally:
        asyncio.run(created.aclose())

    existing_dir = tmp_path / "existing-project-directory"
    existing_dir.mkdir(mode=0o755)
    existing_dir.chmod(0o755)
    existing = SQLiteStorage(existing_dir)
    try:
        assert stat.S_IMODE(existing_dir.stat().st_mode) == 0o755
        assert stat.S_IMODE(existing._database.stat().st_mode) == 0o600
    finally:
        asyncio.run(existing.aclose())


def test_rss_snapshot_pages_are_repeatable_private_and_feed_bound(storage: SQLiteStorage) -> None:
    """Immutable cursor pages preserve all records and bind to a site/feed version."""
    records = [_record(index) for index in range(1, 6)]
    cursor = asyncio.run(storage.create_rss_snapshot("site-a", "feed-v1", records))

    first, next_cursor, has_more = asyncio.run(
        storage.read_rss_snapshot(
            cursor,
            2,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    repeated, repeated_next, repeated_more = asyncio.run(
        storage.read_rss_snapshot(
            cursor,
            1,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [item.source_id for item in first] == ["native-1", "native-2"]
    assert [item.source_id for item in repeated] == ["native-1", "native-2"]
    assert next_cursor == repeated_next
    assert has_more is repeated_more is True
    assert first[0].locator is not None
    assert first[0].locator.get_secret_value().endswith("passkey=private-1")
    first_replayed, first_replay_cursor, first_replay_more = asyncio.run(
        storage.read_rss_snapshot(
            cursor,
            5,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [item.source_id for item in first_replayed] == ["native-1", "native-2"]
    assert first_replay_cursor == next_cursor
    assert first_replay_more is True

    second, cursor_after_second, more_after_second = asyncio.run(
        storage.read_rss_snapshot(
            next_cursor,
            2,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    third, final_cursor, more_after_third = asyncio.run(
        storage.read_rss_snapshot(
            cursor_after_second,
            2,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [item.source_id for item in second] == ["native-3", "native-4"]
    assert [item.source_id for item in third] == ["native-5"]
    assert more_after_second is True
    assert final_cursor is not None
    assert more_after_third is False
    drained, same_cursor, still_drained = asyncio.run(
        storage.read_rss_snapshot(
            final_cursor,
            2,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert drained == []
    assert same_cursor == final_cursor
    assert still_drained is False

    with pytest.raises(NotFound):
        asyncio.run(
            storage.read_rss_snapshot(
                cursor,
                2,
                expected_site_id="site-b",
                expected_feed_version="feed-v1",
            )
        )
    with pytest.raises(NotFound):
        asyncio.run(
            storage.read_rss_snapshot(
                cursor,
                2,
                expected_site_id="site-a",
                expected_feed_version="feed-v2",
            )
        )


def test_rss_next_cursor_can_change_page_limit_without_losing_pending(storage: SQLiteStorage) -> None:
    """Each immutable cursor fixes its own first-read limit, independently of its parent."""
    cursor = asyncio.run(
        storage.create_rss_snapshot("site-a", "feed-v1", [_record(i) for i in range(1, 6)])
    )
    first, next_cursor, has_more = asyncio.run(
        storage.read_rss_snapshot(
            cursor,
            2,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    second, last_cursor, more_after_second = asyncio.run(
        storage.read_rss_snapshot(
            next_cursor,
            1,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    final, _, more_after_final = asyncio.run(
        storage.read_rss_snapshot(
            last_cursor,
            3,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [item.source_id for item in first] == ["native-1", "native-2"]
    assert [item.source_id for item in second] == ["native-3"]
    assert [item.source_id for item in final] == ["native-4", "native-5"]
    assert has_more is True
    assert more_after_second is True
    assert more_after_final is False


def test_rss_incremental_snapshot_carries_seen_and_unreturned_pending(storage: SQLiteStorage) -> None:
    """A drained cursor can discover new feed items without replaying seen resources."""
    initial = asyncio.run(storage.create_rss_snapshot("site-a", "feed-v1", [_record(1), _record(2)]))
    first, next_cursor, has_more = asyncio.run(
        storage.read_rss_snapshot(
            initial,
            1,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [record.source_id for record in first] == ["native-1"]
    assert has_more is True

    # A fresh feed arrives before the old page is drained. The old pending item
    # is carried forward and duplicate current-feed entries are filtered.
    extended = asyncio.run(
        storage.create_rss_snapshot(
            "site-a",
            "feed-v1",
            [_record(2), _record(3)],
            previous_cursor=next_cursor,
        )
    )
    rest, drained_cursor, has_more = asyncio.run(
        storage.read_rss_snapshot(
            extended,
            10,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [record.source_id for record in rest] == ["native-2", "native-3"]
    assert has_more is False
    assert drained_cursor is not None

    # Reusing the older cursor remains a repeatable read; it cannot advance the
    # new reader's snapshot or consume its pending item.
    replay, replay_cursor, replay_more = asyncio.run(
        storage.read_rss_snapshot(
            next_cursor,
            1,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [record.source_id for record in replay] == ["native-2"]
    assert replay_more is False
    replay_again, replay_cursor_again, replay_more_again = asyncio.run(
        storage.read_rss_snapshot(
            next_cursor,
            1,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [record.source_id for record in replay_again] == ["native-2"]
    assert replay_cursor_again == replay_cursor
    assert replay_more_again is False

    newest = asyncio.run(
        storage.create_rss_snapshot(
            "site-a",
            "feed-v1",
            [_record(1), _record(2), _record(3), _record(4)],
            previous_cursor=drained_cursor,
        )
    )
    new_items, _, has_more = asyncio.run(
        storage.read_rss_snapshot(
            newest,
            10,
            expected_site_id="site-a",
            expected_feed_version="feed-v1",
        )
    )
    assert [record.source_id for record in new_items] == ["native-4"]
    assert has_more is False

def test_rss_snapshot_expiration_is_bounded(storage: SQLiteStorage) -> None:
    """A cursor cannot outlive its seven-day maximum snapshot TTL."""
    now = datetime.now(timezone.utc)
    cursor = asyncio.run(
        storage.create_rss_snapshot("site-a", "feed-v1", [_record(1)], ttl_seconds=5)
    )
    with pytest.raises(NotFound):
        asyncio.run(
            storage.read_rss_snapshot(
                cursor,
                1,
                expected_site_id="site-a",
                expected_feed_version="feed-v1",
                now=now + timedelta(seconds=6),
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            storage.create_rss_snapshot(
                "site-a",
                "feed-v1",
                [],
                ttl_seconds=8 * 24 * 60 * 60,
            )
        )


def test_request_receipts_claim_resource_and_hash_until_definitive_finish(storage: SQLiteStorage) -> None:
    """Unknown requests keep cross-request locks; definitive outcomes release them."""
    first = asyncio.run(
        storage.claim_request(
            "request-1",
            "digest-1",
            "qb-default",
            [],
            resource_identity="res_same-resource",
        )
    )
    assert first.created is True
    assert first.hash_claimed is True
    assert first.receipt.state == "claimed"

    duplicate_resource = asyncio.run(
        storage.claim_request(
            "request-2",
            "digest-2",
            "qb-default",
            [],
            resource_identity="res_same-resource",
        )
    )
    assert duplicate_resource.created is False
    assert duplicate_resource.hash_claimed is False
    assert duplicate_resource.receipt.request_id == "request-2"
    assert duplicate_resource.receipt.payload_digest == "digest-2"
    assert duplicate_resource.receipt.state == "failed"
    assert duplicate_resource.receipt.error_code == "resource_in_flight"
    assert asyncio.run(storage.claim_request(
        "request-2", "digest-2", "qb-default", [], resource_identity="res_same-resource"
    )).receipt == duplicate_resource.receipt
    with pytest.raises(IdempotencyConflict):
        asyncio.run(storage.claim_request(
            "request-2", "different-digest", "qb-default", [], resource_identity="res_same-resource"
        ))

    bound = asyncio.run(storage.bind_hashes("request-1", "qb-default", ["A" * 40]))
    assert bound.hash_claimed is True
    assert bound.receipt.hashes == ["a" * 40]
    duplicate_hash = asyncio.run(
        storage.claim_request("request-3", "digest-3", "qb-default", ["a" * 40])
    )
    assert duplicate_hash.created is False
    assert duplicate_hash.receipt.request_id == "request-3"
    assert duplicate_hash.receipt.payload_digest == "digest-3"
    assert duplicate_hash.receipt.state == "failed"
    assert duplicate_hash.receipt.error_code == "hash_in_flight"

    unknown = asyncio.run(storage.finish_request("request-1", state="unknown"))
    assert unknown.state == "unknown"
    still_claimed = asyncio.run(
        storage.claim_request(
            "request-4",
            "digest-4",
            "qb-default",
            ["a" * 40],
            resource_identity="res_same-resource",
        )
    )
    assert still_claimed.created is False
    assert still_claimed.receipt.request_id == "request-4"
    assert still_claimed.receipt.state == "failed"
    assert still_claimed.receipt.error_code == "resource_in_flight"

    finished = asyncio.run(
        storage.finish_request(
            "request-1",
            state="accepted",
            task=DownloadView(hashes=["a" * 40], state="downloading"),
        )
    )
    assert finished.state == "accepted"
    after_finish = asyncio.run(
        storage.claim_request(
            "request-5",
            "digest-5",
            "qb-default",
            ["a" * 40],
            resource_identity="res_same-resource",
        )
    )
    assert after_finish.created is True
    assert asyncio.run(storage.get_request("request-1")).task is not None
    # A blocked request remains failed after the original owner releases its claim.
    replay_blocked = asyncio.run(storage.claim_request(
        "request-2", "digest-2", "qb-default", [], resource_identity="res_same-resource"
    ))
    assert replay_blocked.receipt.request_id == "request-2"
    assert replay_blocked.receipt.state == "failed"


def test_request_id_reuse_with_different_digest_conflicts(storage: SQLiteStorage) -> None:
    """A request identifier cannot be reused for different content."""
    asyncio.run(storage.claim_request("request-1", "digest-1", "qb-default", []))
    with pytest.raises(IdempotencyConflict):
        asyncio.run(storage.claim_request("request-1", "digest-different", "qb-default", []))
    with pytest.raises(IdempotencyConflict):
        asyncio.run(storage.claim_request("request-1", "digest-1", "another-downloader", []))


def test_unknown_receipt_and_claim_survive_storage_reopen(tmp_path: Path) -> None:
    """An uncertain submission remains durable and blocks a duplicate request after restart."""
    data_dir = tmp_path / "state"
    first = SQLiteStorage(data_dir)
    try:
        asyncio.run(
            first.claim_request(
                "uncertain-request",
                "digest",
                "qb-default",
                ["b" * 40],
                resource_identity="res_uncertain",
            )
        )
        asyncio.run(first.finish_request("uncertain-request", state="unknown"))
    finally:
        asyncio.run(first.aclose())

    reopened = SQLiteStorage(data_dir)
    try:
        receipt = asyncio.run(reopened.get_request("uncertain-request"))
        assert receipt is not None
        assert receipt.state == "unknown"
        duplicate = asyncio.run(
            reopened.claim_request(
                "retry-with-new-id",
                "other-digest",
                "qb-default",
                ["b" * 40],
                resource_identity="res_uncertain",
            )
        )
        assert duplicate.created is False
        assert duplicate.receipt.request_id == "retry-with-new-id"
        assert duplicate.receipt.payload_digest == "other-digest"
        assert duplicate.receipt.state == "failed"
        assert duplicate.receipt.error_code == "resource_in_flight"
    finally:
        asyncio.run(reopened.aclose())


def test_claim_request_serializes_competing_instances(storage: SQLiteStorage) -> None:
    """Independent SQLite connections produce one resource owner and one no-post result."""
    second_storage = SQLiteStorage(storage._data_dir)

    async def compete():
        return await asyncio.gather(
            storage.claim_request(
                "request-a", "digest-a", "qb-default", [], resource_identity="res_race"
            ),
            second_storage.claim_request(
                "request-b", "digest-b", "qb-default", [], resource_identity="res_race"
            ),
        )

    try:
        outcomes = asyncio.run(compete())
        assert sum(outcome.created for outcome in outcomes) == 1
        assert sum(not outcome.hash_claimed for outcome in outcomes) == 1
        loser = next(outcome for outcome in outcomes if not outcome.created)
        assert loser.receipt.request_id in {"request-a", "request-b"}
        assert loser.receipt.payload_digest in {"digest-a", "digest-b"}
        assert loser.receipt.state == "failed"
        assert loser.receipt.error_code == "resource_in_flight"
        assert asyncio.run(second_storage.get_request(loser.receipt.request_id)) == loser.receipt
        with pytest.raises(IdempotencyConflict):
            asyncio.run(second_storage.claim_request(
                loser.receipt.request_id, "changed-digest", "qb-default", [], resource_identity="res_race"
            ))
    finally:
        asyncio.run(second_storage.aclose())


def test_bind_hash_conflict_persists_own_receipt_across_connections(storage: SQLiteStorage) -> None:
    second_storage = SQLiteStorage(storage._data_dir)
    try:
        first = asyncio.run(storage.claim_request(
            "hash-owner", "owner-digest", "qb-default", ["c" * 40]
        ))
        contender = asyncio.run(second_storage.claim_request(
            "hash-contender", "contender-digest", "qb-default", [], resource_identity="res_contender"
        ))
        assert first.created and contender.created
        conflict = asyncio.run(second_storage.bind_hashes(
            "hash-contender", "qb-default", ["c" * 40]
        ))
        assert conflict.hash_claimed is False
        assert conflict.receipt.request_id == "hash-contender"
        assert conflict.receipt.payload_digest == "contender-digest"
        assert conflict.receipt.state == "failed"
        assert conflict.receipt.error_code == "hash_in_flight"

        asyncio.run(storage.finish_request("hash-owner", state="accepted"))
        replay = asyncio.run(second_storage.claim_request(
            "hash-contender", "contender-digest", "qb-default", ["c" * 40]
        ))
        assert replay.receipt == conflict.receipt
        assert replay.receipt.state == "failed"
        with pytest.raises(IdempotencyConflict):
            asyncio.run(second_storage.claim_request(
                "hash-contender", "new-digest", "qb-default", ["c" * 40]
            ))
    finally:
        asyncio.run(second_storage.aclose())
