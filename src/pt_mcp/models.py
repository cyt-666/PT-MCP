"""Typed public and private contracts for sites, resources, and downloads."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Generic, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

T = TypeVar("T")


def make_resource_id(site_id: str, source_id: str) -> str:
    """Create a stable opaque service ID from a site and its native resource ID."""
    digest = hashlib.sha256(f"{site_id}\0{source_id}".encode("utf-8")).hexdigest()
    return f"res_{digest[:40]}"


class StrictModel(BaseModel):
    """Reject unknown configuration and response fields by default."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class NativeCategory(StrictModel):
    """A category exactly as exposed by one torrent site."""

    id: str
    label: str


class SiteConfig(StrictModel):
    """Explicit site profile and private configuration reference."""

    site_id: str
    name: str
    kind: Literal["nexusphp", "mteam"]
    base_url: str
    api_base_url: str | None = None
    profile: str | None = None
    credential_ref: str | None = None
    rss_url_env: str | None = None
    native_categories: list[NativeCategory] = Field(default_factory=list)
    download_hosts: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=15.0, gt=0, le=120)
    proxy: str | None = None
    config_version: str = "1"

    @field_validator("site_id", "name", "kind", "config_version")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        """Reject blank identifiers that break storage keys or profile lookup."""
        if not value.strip():
            raise ValueError("value must not be empty")
        return value.strip()

    @field_validator("base_url", "api_base_url")
    @classmethod
    def validate_site_url(cls, value: str | None) -> str | None:
        """Require a credential-free HTTP(S) origin for site requests."""
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("site URLs must be credential-free HTTP(S) URLs")
        return value.rstrip("/")


class ResourceRecord(StrictModel):
    """Private cached resource; locator never appears in ordinary model dumps."""

    site_id: str
    resource_id: str
    source_id: str = Field(repr=False, exclude=True)
    public_source_id: str | None = None
    native_categories: list[NativeCategory] = Field(default_factory=list)
    title: str
    description: str | None = None
    detail_url: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    seeders: int | None = Field(default=None, ge=0)
    peers: int | None = Field(default=None, ge=0)
    published_at: datetime | None = None
    downloadvolumefactor: float | None = Field(default=None, ge=0)
    uploadvolumefactor: float | None = Field(default=None, ge=0)
    hit_and_run: bool | None = None
    labels: list[str] = Field(default_factory=list)
    locator: SecretStr | None = Field(default=None, repr=False, exclude=True)
    config_version: str = "1"
    expires_at: datetime | None = None

    @classmethod
    def from_storage(cls, payload: dict[str, object]) -> "ResourceRecord":
        """Restore private locator data from the protected local database."""
        values = dict(payload)
        source_id = values.pop("_private_source_id", None)
        if source_id is not None:
            values["source_id"] = str(source_id)
        locator = values.pop("_private_locator", None)
        if locator is not None:
            values["locator"] = SecretStr(str(locator))
        return cls.model_validate(values)

    def to_storage(self) -> dict[str, object]:
        """Serialize a private row for the mode-0600 local SQLite database."""
        payload = self.model_dump(mode="json")
        payload["_private_source_id"] = self.source_id
        payload["_private_locator"] = self.locator.get_secret_value() if self.locator else None
        return payload


class ResourceView(StrictModel):
    """Public resource projection; it deliberately has no locator or credentials."""

    site_id: str
    resource_id: str
    source_id: str | None = None
    native_categories: list[NativeCategory] = Field(default_factory=list)
    title: str
    description: str | None = None
    detail_url: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    seeders: int | None = Field(default=None, ge=0)
    peers: int | None = Field(default=None, ge=0)
    published_at: datetime | None = None
    downloadvolumefactor: float | None = Field(default=None, ge=0)
    uploadvolumefactor: float | None = Field(default=None, ge=0)
    hit_and_run: bool | None = None
    labels: list[str] = Field(default_factory=list)

    @classmethod
    def from_record(cls, record: ResourceRecord) -> "ResourceView":
        """Project a cached resource without private source IDs or locator data."""
        public_fields = {
            "site_id",
            "resource_id",
            "public_source_id",
            "native_categories",
            "title",
            "description",
            "detail_url",
            "size_bytes",
            "seeders",
            "peers",
            "published_at",
            "downloadvolumefactor",
            "uploadvolumefactor",
            "hit_and_run",
            "labels",
        }
        payload = record.model_dump(include=public_fields)
        payload["source_id"] = payload.pop("public_source_id", None)
        return cls.model_validate(payload)


class SearchPage(StrictModel, Generic[T]):
    """One adapter page, preserving uncertainty about whether another page exists."""

    items: list[T]
    page: int = Field(ge=1)
    has_more: bool | None = None


class RssReadPage(StrictModel):
    """A caller-owned immutable RSS snapshot page."""

    items: list[ResourceView]
    cursor: str | None = None
    has_more: bool


class SiteSummary(StrictModel):
    """Credential-free site discovery result."""

    site_id: str
    name: str
    kind: str
    base_url: str
    categories: list[NativeCategory] = Field(default_factory=list)


class DownloaderConfig(StrictModel):
    """Optional qBittorrent connection and named server-side save profiles."""

    downloader_id: str = "default"
    kind: Literal["qbittorrent"] = "qbittorrent"
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    credential_ref: str | None = None
    verify_tls: bool = True
    timeout_s: float = Field(default=15.0, gt=0, le=120)
    save_profiles: dict[str, str] = Field(default_factory=dict)
    default_save_profile_id: str | None = None


class DownloaderSummary(StrictModel):
    """Credential-free downloader discovery result."""

    downloader_id: str
    kind: str
    configured: bool


class DownloadAck(StrictModel):
    """A positive downloader acknowledgement, not a resource identity."""

    accepted: bool
    task_id: str | None = None


class DownloadView(StrictModel):
    """A downloader task projected by its hashes, never by title matching."""

    hashes: list[str] = Field(default_factory=list)
    name: str | None = None
    state: str | None = None
    progress: float | None = Field(default=None, ge=0, le=100)
    size_bytes: int | None = Field(default=None, ge=0)
    download_dir: str | None = None


class SubmissionReceipt(StrictModel):
    """Durable idempotency receipt for an add request."""

    request_id: str
    payload_digest: str
    state: Literal["claimed", "accepted", "already_exists", "unknown", "failed"]
    downloader_id: str
    hashes: list[str] = Field(default_factory=list)
    task: DownloadView | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class RssItem(StrictModel):
    """Raw feed record before it is matched to a known site resource."""

    stable_ref: str
    title: str
    description: str | None = None
    link: str | None = None
    enclosure: str | None = None
    native_category: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    published_at: datetime | None = None
    guid: str | None = None
