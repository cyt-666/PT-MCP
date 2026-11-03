"""Atomic local SQLite persistence for private resources and operation receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

from pt_mcp.errors import ConfigError, IdempotencyConflict, NotFound
from pt_mcp.models import (
    DownloadView,
    ResourceRecord,
    SubmissionReceipt,
)

DEFAULT_RESOURCE_TTL_SECONDS = 24 * 60 * 60
MAX_RESOURCE_TTL_SECONDS = DEFAULT_RESOURCE_TTL_SECONDS
DEFAULT_RSS_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_RSS_TTL_SECONDS = DEFAULT_RSS_TTL_SECONDS
MAX_RSS_PAGE_SIZE = 500
_UMASK_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Result of atomically claiming a request, identity, or torrent hash."""

    created: bool
    receipt: SubmissionReceipt
    hash_claimed: bool


class SQLiteStorage:
    """Store resource records, immutable RSS snapshots, and idempotency receipts.

    The SQLite connection is serialized by a thread lock. Async methods offload
    short database transactions to ``asyncio.to_thread``; no network operation
    runs inside a transaction.
    """

    def __init__(self, data_dir: Path) -> None:
        """Create the private state directory only when absent and initialize WAL."""
        self._data_dir = Path(data_dir).expanduser()
        existed = self._data_dir.exists()
        try:
            if self._data_dir.is_symlink():
                raise ConfigError("存储目录不能是符号链接")
            self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self._data_dir.is_dir():
                raise ConfigError("存储路径不是目录")
            if not existed:
                os.chmod(self._data_dir, 0o700)
        except OSError:
            raise ConfigError("无法创建本地私有存储目录") from None

        self._database = self._data_dir / "state.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._create_database_file()
        try:
            # SQLite creates WAL/SHM sidecars itself. A restrictive umask during
            # the one-time synchronous startup prevents a permissive creation
            # mode before the explicit chmod below.
            with _UMASK_LOCK:
                old_umask = os.umask(0o077)
                try:
                    self._connection = sqlite3.connect(
                        self._database,
                        timeout=5.0,
                        isolation_level=None,
                        check_same_thread=False,
                    )
                    self._connection.row_factory = sqlite3.Row
                    self._connection.execute("PRAGMA foreign_keys = ON")
                    self._connection.execute("PRAGMA busy_timeout = 5000")
                    self._connection.execute("PRAGMA journal_mode = WAL")
                    self._connection.execute("PRAGMA synchronous = FULL")
                    self._initialize_schema()
                    self._secure_database_files()
                finally:
                    os.umask(old_umask)
        except sqlite3.Error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise ConfigError("无法初始化本地 SQLite 存储") from None

    async def put_resources(
        self,
        records: Sequence[ResourceRecord],
        *,
        ttl_seconds: int = DEFAULT_RESOURCE_TTL_SECONDS,
    ) -> None:
        """Upsert private records with a bounded lifetime and config fingerprint."""
        if not 0 < ttl_seconds <= MAX_RESOURCE_TTL_SECONDS:
            raise ValueError("resource TTL must be between 1 and 86400 seconds")
        await self._run(self._put_resources, list(records), ttl_seconds)

    async def get_resource(
        self,
        resource_id: str,
        *,
        config_version: str | None = None,
        now: datetime | None = None,
    ) -> ResourceRecord | None:
        """Read by opaque ID first; optionally validate a known site's config fingerprint."""
        if not resource_id:
            raise ValueError("resource_id is required")
        current_time = _timestamp(now)
        return await self._run(self._get_resource, resource_id, config_version, current_time)

    async def claim_request(
        self,
        request_id: str,
        payload_digest: str,
        downloader_id: str,
        hashes: Sequence[str],
        *,
        resource_identity: str | None = None,
    ) -> ClaimResult:
        """Persist the idempotency receipt and cross-request in-flight claims atomically."""
        if not request_id or not payload_digest or not downloader_id:
            raise ValueError("request_id, payload_digest, and downloader_id are required")
        normalized_hashes = _normalize_hashes(hashes)
        return await self._run(
            self._claim_request,
            request_id,
            payload_digest,
            downloader_id,
            normalized_hashes,
            resource_identity,
        )

    async def bind_hashes(
        self,
        request_id: str,
        downloader_id: str,
        hashes: Sequence[str],
    ) -> ClaimResult:
        """Atomically bind hashes learned after download resolution to an existing request."""
        if not request_id or not downloader_id:
            raise ValueError("request_id and downloader_id are required")
        return await self._run(
            self._bind_hashes,
            request_id,
            downloader_id,
            _normalize_hashes(hashes),
        )

    async def finish_request(
        self,
        request_id: str,
        *,
        state: str,
        task: DownloadView | None = None,
        error_code: str | None = None,
    ) -> SubmissionReceipt:
        """Persist an operation result; unknown outcomes retain in-flight claims."""
        if state not in {"accepted", "already_exists", "unknown", "failed"}:
            raise ValueError("invalid submission state")
        return await self._run(self._finish_request, request_id, state, task, error_code)

    async def get_request(self, request_id: str) -> SubmissionReceipt | None:
        """Read a durable request receipt without retrying external submission."""
        if not request_id:
            raise ValueError("request_id is required")
        return await self._run(self._get_request, request_id)

    async def create_rss_snapshot(
        self,
        site_id: str,
        feed_version: str,
        items: Sequence[ResourceRecord],
        *,
        ttl_seconds: int = DEFAULT_RSS_TTL_SECONDS,
        previous_cursor: str | None = None,
    ) -> str:
        """Create an immutable feed snapshot, continuing seen/pending state when supplied."""
        if not site_id or not feed_version:
            raise ValueError("site_id and feed_version are required")
        if not 0 < ttl_seconds <= MAX_RSS_TTL_SECONDS:
            raise ValueError("RSS snapshot TTL must be between 1 and 604800 seconds")
        records = list(items)
        if any(item.site_id != site_id for item in records):
            raise ValueError("all RSS records must belong to the snapshot site")
        return await self._run(
            self._create_rss_snapshot,
            site_id,
            feed_version,
            records,
            ttl_seconds,
            previous_cursor,
        )

    async def read_rss_snapshot(
        self,
        cursor: str,
        limit: int,
        *,
        expected_site_id: str,
        expected_feed_version: str,
        now: datetime | None = None,
    ) -> tuple[list[ResourceRecord], str | None, bool]:
        """Read an immutable page; repeated reads with a cursor return identical contents."""
        if not cursor or not expected_site_id or not expected_feed_version:
            raise ValueError("cursor, expected_site_id, and expected_feed_version are required")
        if not 1 <= limit <= MAX_RSS_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_RSS_PAGE_SIZE}")
        return await self._run(
            self._read_rss_snapshot,
            cursor,
            limit,
            expected_site_id,
            expected_feed_version,
            _timestamp(now),
        )

    async def aclose(self) -> None:
        """Checkpoint WAL and close the local connection."""
        if not self._closed:
            await self._run(self._close)

    async def _run(self, function, *args):
        """Run a short thread-locked database operation outside the event loop."""
        try:
            return await asyncio.to_thread(function, *args)
        except sqlite3.Error:
            raise ConfigError("本地存储操作失败") from None

    def _create_database_file(self) -> None:
        """Create or secure the app-owned database without changing parent permissions."""
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._database, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ConfigError("SQLite 路径不是普通文件")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError:
            raise ConfigError("无法创建本地 SQLite 文件") from None

    def _initialize_schema(self) -> None:
        """Create private resource, RSS snapshot, receipt, and in-flight claim tables."""
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    private_locator TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_resources_site_expiry
                    ON resources(site_id, expires_at);

                CREATE TABLE IF NOT EXISTS rss_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    feed_version TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    seen_json TEXT NOT NULL,
                    pending_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rss_cursors (
                    token_hash TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES rss_snapshots(snapshot_id) ON DELETE CASCADE,
                    page_limit INTEGER,
                    next_token TEXT,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rss_snapshots_expiry ON rss_snapshots(expires_at);

                CREATE TABLE IF NOT EXISTS submission_receipts (
                    request_id TEXT PRIMARY KEY,
                    payload_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    downloader_id TEXT NOT NULL,
                    hashes_json TEXT NOT NULL,
                    task_json TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submission_resource_claims (
                    downloader_id TEXT NOT NULL,
                    resource_identity TEXT NOT NULL,
                    request_id TEXT NOT NULL REFERENCES submission_receipts(request_id) ON DELETE CASCADE,
                    PRIMARY KEY(downloader_id, resource_identity)
                );
                CREATE TABLE IF NOT EXISTS submission_hash_claims (
                    downloader_id TEXT NOT NULL,
                    torrent_hash TEXT NOT NULL,
                    request_id TEXT NOT NULL REFERENCES submission_receipts(request_id) ON DELETE CASCADE,
                    PRIMARY KEY(downloader_id, torrent_hash)
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold the per-instance lock for a short immediate SQLite transaction."""
        with self._lock:
            if self._closed:
                raise ConfigError("本地存储已经关闭")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current_time = _utcnow().timestamp()
                self._connection.execute("DELETE FROM resources WHERE expires_at <= ?", (current_time,))
                self._connection.execute("DELETE FROM rss_snapshots WHERE expires_at <= ?", (current_time,))
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            finally:
                self._secure_database_files()

    def _put_resources(self, records: list[ResourceRecord], ttl_seconds: int) -> None:
        now = _utcnow()
        expiry = now + timedelta(seconds=ttl_seconds)
        with self._transaction() as connection:
            for record in records:
                stored_record = record.model_copy(update={"expires_at": expiry})
                values = stored_record.to_storage()
                private_locator = values.pop("_private_locator", None)
                payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO resources(resource_id, site_id, config_version, expires_at, payload_json, private_locator)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(resource_id) DO UPDATE SET
                           site_id=excluded.site_id,
                           config_version=excluded.config_version,
                           expires_at=excluded.expires_at,
                           payload_json=excluded.payload_json,
                           private_locator=excluded.private_locator""",
                    (
                        stored_record.resource_id,
                        stored_record.site_id,
                        stored_record.config_version,
                        expiry.timestamp(),
                        payload,
                        private_locator,
                    ),
                )

    def _get_resource(
        self,
        resource_id: str,
        config_version: str | None,
        now: float,
    ) -> ResourceRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                connection.execute("DELETE FROM resources WHERE resource_id = ?", (resource_id,))
                return None
            if config_version is not None and row["config_version"] != config_version:
                return None
            values = json.loads(row["payload_json"])
            values["_private_locator"] = row["private_locator"]
            return ResourceRecord.from_storage(values)

    def _claim_request(
        self,
        request_id: str,
        payload_digest: str,
        downloader_id: str,
        hashes: list[str],
        resource_identity: str | None,
    ) -> ClaimResult:
        now = _utcnow().isoformat()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM submission_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_digest"] != payload_digest
                    or existing["downloader_id"] != downloader_id
                ):
                    raise IdempotencyConflict()
                receipt = _receipt_from_row(existing)
                return ClaimResult(
                    created=False,
                    receipt=receipt,
                    hash_claimed=receipt.state not in {"accepted", "already_exists", "failed"},
                )

            owner_request_id = self._find_claim_owner(
                connection,
                downloader_id,
                hashes,
                resource_identity,
            )
            if owner_request_id is not None:
                owner_row = connection.execute(
                    "SELECT * FROM submission_receipts WHERE request_id = ?",
                    (owner_request_id,),
                ).fetchone()
                if owner_row is None:
                    raise ConfigError("下载请求占用状态不一致")
                error_code = "resource_in_flight" if resource_identity else "hash_in_flight"
                connection.execute(
                    """INSERT INTO submission_receipts
                       (request_id, payload_digest, state, downloader_id, hashes_json, task_json,
                        error_code, created_at, updated_at)
                       VALUES (?, ?, 'failed', ?, ?, NULL, ?, ?, ?)""",
                    (request_id, payload_digest, downloader_id, _json(hashes), error_code, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM submission_receipts WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                return ClaimResult(created=False, receipt=_receipt_from_row(row), hash_claimed=False)

            connection.execute(
                """INSERT INTO submission_receipts
                   (request_id, payload_digest, state, downloader_id, hashes_json, task_json,
                    error_code, created_at, updated_at)
                   VALUES (?, ?, 'claimed', ?, ?, NULL, NULL, ?, ?)""",
                (request_id, payload_digest, downloader_id, _json(hashes), now, now),
            )
            if resource_identity:
                connection.execute(
                    "INSERT INTO submission_resource_claims(downloader_id, resource_identity, request_id) VALUES (?, ?, ?)",
                    (downloader_id, resource_identity, request_id),
                )
            for torrent_hash in hashes:
                connection.execute(
                    "INSERT INTO submission_hash_claims(downloader_id, torrent_hash, request_id) VALUES (?, ?, ?)",
                    (downloader_id, torrent_hash, request_id),
                )
            row = connection.execute(
                "SELECT * FROM submission_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return ClaimResult(created=True, receipt=_receipt_from_row(row), hash_claimed=True)

    def _bind_hashes(self, request_id: str, downloader_id: str, hashes: list[str]) -> ClaimResult:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM submission_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise NotFound("下载请求不存在")
            receipt = _receipt_from_row(row)
            if receipt.downloader_id != downloader_id:
                raise IdempotencyConflict()
            if receipt.state in {"accepted", "already_exists", "failed"}:
                return ClaimResult(created=False, receipt=receipt, hash_claimed=False)

            owner_request_id = self._find_claim_owner(connection, downloader_id, hashes, None)
            if owner_request_id is not None and owner_request_id != request_id:
                owner_row = connection.execute(
                    "SELECT * FROM submission_receipts WHERE request_id = ?",
                    (owner_request_id,),
                ).fetchone()
                if owner_row is None:
                    raise ConfigError("下载哈希占用状态不一致")
                now = _utcnow().isoformat()
                connection.execute(
                    "UPDATE submission_receipts SET state='failed', error_code='hash_in_flight', updated_at=? WHERE request_id=?",
                    (now, request_id),
                )
                connection.execute("DELETE FROM submission_resource_claims WHERE request_id=?", (request_id,))
                connection.execute("DELETE FROM submission_hash_claims WHERE request_id=?", (request_id,))
                failed = connection.execute(
                    "SELECT * FROM submission_receipts WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                return ClaimResult(created=False, receipt=_receipt_from_row(failed), hash_claimed=False)

            merged_hashes = sorted(set(receipt.hashes) | set(hashes))
            for torrent_hash in hashes:
                connection.execute(
                    "INSERT OR IGNORE INTO submission_hash_claims(downloader_id, torrent_hash, request_id) VALUES (?, ?, ?)",
                    (downloader_id, torrent_hash, request_id),
                )
            connection.execute(
                "UPDATE submission_receipts SET hashes_json=?, updated_at=? WHERE request_id=?",
                (_json(merged_hashes), _utcnow().isoformat(), request_id),
            )
            updated = connection.execute(
                "SELECT * FROM submission_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return ClaimResult(created=False, receipt=_receipt_from_row(updated), hash_claimed=True)

    @staticmethod
    def _find_claim_owner(
        connection: sqlite3.Connection,
        downloader_id: str,
        hashes: list[str],
        resource_identity: str | None,
    ) -> str | None:
        if resource_identity:
            row = connection.execute(
                "SELECT request_id FROM submission_resource_claims WHERE downloader_id=? AND resource_identity=?",
                (downloader_id, resource_identity),
            ).fetchone()
            if row is not None:
                return str(row["request_id"])
        for torrent_hash in hashes:
            row = connection.execute(
                "SELECT request_id FROM submission_hash_claims WHERE downloader_id=? AND torrent_hash=?",
                (downloader_id, torrent_hash),
            ).fetchone()
            if row is not None:
                return str(row["request_id"])
        return None

    def _finish_request(
        self,
        request_id: str,
        state: str,
        task: DownloadView | None,
        error_code: str | None,
    ) -> SubmissionReceipt:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM submission_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise NotFound("下载请求不存在")
            current_state = row["state"]
            terminal_states = {"accepted", "already_exists", "failed"}
            if current_state in terminal_states and state != current_state:
                raise ConfigError("已完成的下载请求不能被回退")
            if current_state == "unknown" and state not in {
                "unknown", "accepted", "already_exists", "failed"
            }:
                raise ConfigError("结果未知的请求只能通过核验更新")
            task_json = _json(task.model_dump(mode="json")) if task is not None else None
            connection.execute(
                "UPDATE submission_receipts SET state=?, task_json=?, error_code=?, updated_at=? WHERE request_id=?",
                (state, task_json, error_code, _utcnow().isoformat(), request_id),
            )
            if state in {"accepted", "already_exists", "failed"}:
                connection.execute("DELETE FROM submission_resource_claims WHERE request_id=?", (request_id,))
                connection.execute("DELETE FROM submission_hash_claims WHERE request_id=?", (request_id,))
            updated = connection.execute(
                "SELECT * FROM submission_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return _receipt_from_row(updated)

    def _get_request(self, request_id: str) -> SubmissionReceipt | None:
        with self._lock:
            if self._closed:
                raise ConfigError("本地存储已经关闭")
            row = self._connection.execute(
                "SELECT * FROM submission_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return _receipt_from_row(row) if row is not None else None

    def _create_rss_snapshot(
        self,
        site_id: str,
        feed_version: str,
        items: list[ResourceRecord],
        ttl_seconds: int,
        previous_cursor: str | None = None,
    ) -> str:
        now = _utcnow().timestamp()
        expires_at = now + ttl_seconds
        snapshot_id = secrets.token_urlsafe(24)
        cursor = secrets.token_urlsafe(32)
        with self._transaction() as connection:
            seen_refs: list[str] = []
            pending_records: list[dict[str, object]] = []
            if previous_cursor:
                previous = connection.execute(
                    """SELECT c.expires_at AS cursor_expires_at, s.site_id, s.feed_version,
                              s.expires_at AS snapshot_expires_at, s.seen_json, s.pending_json
                       FROM rss_cursors c JOIN rss_snapshots s USING(snapshot_id)
                       WHERE c.token_hash=?""",
                    (_token_hash(previous_cursor),),
                ).fetchone()
                if previous is None or previous["cursor_expires_at"] <= now or previous["snapshot_expires_at"] <= now:
                    raise NotFound("RSS 游标不存在或已过期")
                if previous["site_id"] != site_id or previous["feed_version"] != feed_version:
                    raise NotFound("RSS 游标与当前站点配置不匹配")
                seen_refs = list(json.loads(previous["seen_json"]))
                pending_records = list(json.loads(previous["pending_json"]))

            known_refs = set(seen_refs)
            known_refs.update(_record_identity(ResourceRecord.from_storage(item)) for item in pending_records)
            for record in items:
                identity = _record_identity(record)
                if identity in known_refs:
                    continue
                pending_records.append(record.to_storage())
                known_refs.add(identity)
            connection.execute(
                """INSERT INTO rss_snapshots
                   (snapshot_id, site_id, feed_version, created_at, expires_at, seen_json, pending_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, site_id, feed_version, now, expires_at, _json(seen_refs), _json(pending_records)),
            )
            connection.execute(
                "INSERT INTO rss_cursors(token_hash, snapshot_id, page_limit, next_token, expires_at) VALUES (?, ?, NULL, NULL, ?)",
                (_token_hash(cursor), snapshot_id, expires_at),
            )
        return cursor

    def _read_rss_snapshot(
        self,
        cursor: str,
        limit: int,
        expected_site_id: str,
        expected_feed_version: str,
        now: float,
    ) -> tuple[list[ResourceRecord], str | None, bool]:
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT c.token_hash, c.snapshot_id, c.page_limit, c.next_token,
                          c.expires_at AS cursor_expires_at, s.site_id, s.feed_version,
                          s.created_at AS snapshot_created_at, s.expires_at AS snapshot_expires_at,
                          s.seen_json, s.pending_json
                   FROM rss_cursors c JOIN rss_snapshots s USING(snapshot_id)
                   WHERE c.token_hash = ?""",
                (_token_hash(cursor),),
            ).fetchone()
            if row is None:
                raise NotFound("RSS 游标不存在或已过期")
            if row["cursor_expires_at"] <= now or row["snapshot_expires_at"] <= now:
                raise NotFound("RSS 游标不存在或已过期")
            if row["site_id"] != expected_site_id or row["feed_version"] != expected_feed_version:
                raise NotFound("RSS 游标与当前站点配置不匹配")

            pending = list(json.loads(row["pending_json"]))
            if not pending:
                # A drained cursor remains a stable continuation point. The caller
                # can fetch the current feed and create a new snapshot from it.
                return [], cursor, False
            page_limit = row["page_limit"]
            if page_limit is None:
                page_limit = limit
                connection.execute(
                    "UPDATE rss_cursors SET page_limit=? WHERE token_hash=?",
                    (page_limit, row["token_hash"]),
                )
            end = min(int(page_limit), len(pending))
            page_records = [ResourceRecord.from_storage(item) for item in pending[:end]]
            remaining_records = pending[end:]
            seen_refs = list(json.loads(row["seen_json"]))
            seen_set = set(seen_refs)
            for record in page_records:
                identity = _record_identity(record)
                if identity not in seen_set:
                    seen_refs.append(identity)
                    seen_set.add(identity)

            next_cursor = row["next_token"]
            if not next_cursor:
                next_cursor = secrets.token_urlsafe(32)
                next_snapshot_id = secrets.token_urlsafe(24)
                connection.execute(
                    """INSERT INTO rss_snapshots
                       (snapshot_id, site_id, feed_version, created_at, expires_at, seen_json, pending_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        next_snapshot_id,
                        row["site_id"],
                        row["feed_version"],
                        row["snapshot_created_at"],
                        row["snapshot_expires_at"],
                        _json(seen_refs),
                        _json(remaining_records),
                    ),
                )
                connection.execute(
                    "INSERT INTO rss_cursors(token_hash, snapshot_id, page_limit, next_token, expires_at) VALUES (?, ?, NULL, NULL, ?)",
                    (_token_hash(next_cursor), next_snapshot_id, row["cursor_expires_at"]),
                )
                connection.execute(
                    "UPDATE rss_cursors SET next_token=? WHERE token_hash=?",
                    (next_cursor, row["token_hash"]),
                )
            return page_records, next_cursor, bool(remaining_records)

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._connection.close()
            self._closed = True
            self._secure_database_files()

    def _secure_database_files(self) -> None:
        for path in (self._database, Path(f"{self._database}-wal"), Path(f"{self._database}-shm")):
            try:
                if path.is_symlink():
                    raise ConfigError("SQLite 数据文件不能是符号链接")
                if path.is_file():
                    os.chmod(path, 0o600)
            except OSError:
                raise ConfigError("无法保护本地 SQLite 文件权限") from None


def _receipt_from_row(row: sqlite3.Row) -> SubmissionReceipt:
    """Restore a credential-free durable receipt from one SQL row."""
    task_data = json.loads(row["task_json"]) if row["task_json"] else None
    return SubmissionReceipt(
        request_id=row["request_id"],
        payload_digest=row["payload_digest"],
        state=row["state"],
        downloader_id=row["downloader_id"],
        hashes=json.loads(row["hashes_json"]),
        task=DownloadView.model_validate(task_data) if task_data is not None else None,
        error_code=row["error_code"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _normalize_hashes(values: Sequence[str]) -> list[str]:
    """Normalize and deduplicate qBittorrent hashes before unique claiming."""
    normalized = sorted({str(value).strip().lower() for value in values if str(value).strip()})
    return normalized


def _record_identity(record: ResourceRecord) -> str:
    """Use the adapter's stable private source identity for RSS seen/pending state."""
    return record.source_id or record.resource_id


def _json(value: object) -> str:
    """Serialize already-validated private JSON payloads deterministically."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _token_hash(token: str) -> str:
    """Hash a bearer cursor before storing its lookup key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None) -> float:
    """Convert optional clock input to a UTC epoch timestamp."""
    if value is None:
        return _utcnow().timestamp()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


__all__ = ["ClaimResult", "SQLiteStorage"]
