"""Typed, explicit process and site configuration loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from pt_mcp.credentials import CredentialReference
from pt_mcp.errors import ConfigError
from pt_mcp.models import DownloaderConfig, SiteConfig, StrictModel


class AppConfig(StrictModel):
    """Server settings; sites and downloaders may both be empty at startup."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    path: str = "/mcp"
    bearer_token: SecretStr | None = Field(default=None, repr=False)
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".local" / "share" / "pt-mcp")
    profile_dir: Path = Field(default_factory=lambda: Path.cwd())
    sites: list[SiteConfig] = Field(default_factory=list)
    credentials: dict[str, CredentialReference] = Field(default_factory=dict)
    downloaders: list[DownloaderConfig] = Field(default_factory=list)
    allowed_hosts: list[str] | None = None
    allowed_origins: list[str] | None = None
    site_concurrency: int = Field(default=2, ge=1, le=32)
    min_site_interval_s: float = Field(default=1.0, ge=0, le=300)
    request_timeout_s: float = Field(default=15.0, gt=0, le=120)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value or not value.strip() or any(char in value for char in "/?#@"):
            raise ValueError("host must be a hostname or IP address")
        return value.strip()

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/") or "?" in value or "#" in value:
            raise ValueError("path must be an absolute route path")
        return value

    @model_validator(mode="after")
    def require_unique_ids(self) -> "AppConfig":
        site_ids = [site.site_id for site in self.sites]
        downloader_ids = [item.downloader_id for item in self.downloaders]
        if len(site_ids) != len(set(site_ids)):
            raise ValueError("site_id values must be unique")
        if len(downloader_ids) != len(set(downloader_ids)):
            raise ValueError("downloader_id values must be unique")
        return self

    @property
    def resolved_allowed_hosts(self) -> list[str]:
        """Return explicit host rules or loopback-only defaults for this port."""
        if self.allowed_hosts is not None:
            return list(self.allowed_hosts)
        port = self.port
        return [f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"]

    @property
    def resolved_allowed_origins(self) -> list[str]:
        """Return explicit origins or loopback-only defaults for this port."""
        if self.allowed_origins is not None:
            return list(self.allowed_origins)
        port = self.port
        return [
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
            f"http://[::1]:{port}",
        ]


def load_config(
    config_path: str | Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> AppConfig:
    """Load optional YAML settings, then apply token and CLI/environment overrides.

    ``PT_MCP_CONFIG`` selects the configuration file when ``config_path`` is absent.
    The bearer token is read only from ``PT_MCP_TOKEN`` and is never sourced from YAML.
    """
    selected_path = config_path or os.environ.get("PT_MCP_CONFIG")
    values: dict[str, Any] = {}
    if selected_path:
        path = Path(selected_path).expanduser()
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ConfigError("配置文件不存在") from error
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise ConfigError("配置文件无法读取或格式无效") from error
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError("配置文件根节点必须是对象")
        values = dict(loaded)
        values.setdefault("profile_dir", path.resolve().parent)

    if "bearer_token" in values:
        raise ConfigError("MCP Bearer token 只能通过 PT_MCP_TOKEN 环境变量提供")

    if token := os.environ.get("PT_MCP_TOKEN"):
        values["bearer_token"] = SecretStr(token)
    if host is not None:
        values["host"] = host
    elif env_host := os.environ.get("PT_MCP_HOST"):
        values["host"] = env_host
    if port is not None:
        values["port"] = port
    elif env_port := os.environ.get("PT_MCP_PORT"):
        try:
            values["port"] = int(env_port)
        except ValueError as error:
            raise ConfigError("PT_MCP_PORT 必须是整数") from error

    try:
        return AppConfig.model_validate(values)
    except ValidationError as error:
        raise ConfigError("配置文件包含无效字段或值") from error
