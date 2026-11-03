"""Lazy credential references backed by environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

from pydantic import SecretStr

from pt_mcp.models import StrictModel


class CredentialReference(StrictModel):
    """Names of environment variables that hold one site's credentials."""

    cookie_env: str | None = None
    api_key_env: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    rss_url_env: str | None = None


class SiteCredentials(StrictModel):
    """Credentials resolved only for the operation that needs them."""

    cookie: SecretStr | None = None
    api_key: SecretStr | None = None
    username: SecretStr | None = None
    password: SecretStr | None = None
    rss_url: SecretStr | None = None


class CredentialProvider(Protocol):
    """The smallest credential lookup port used by site adapters."""

    async def get(self, reference: str | None) -> SiteCredentials | None:
        """Resolve a local reference, or return None when it is unavailable."""


class EnvironmentCredentialProvider:
    """Resolve configured environment variable names without eager secret reads."""

    def __init__(self, references: Mapping[str, CredentialReference]) -> None:
        self._references = dict(references)

    async def get(self, reference: str | None) -> SiteCredentials | None:
        if not reference:
            return None
        spec = self._references.get(reference)
        if spec is None:
            return None
        cookie = os.environ.get(spec.cookie_env) if spec.cookie_env else None
        api_key = os.environ.get(spec.api_key_env) if spec.api_key_env else None
        username = os.environ.get(spec.username_env) if spec.username_env else None
        password = os.environ.get(spec.password_env) if spec.password_env else None
        rss_url = os.environ.get(spec.rss_url_env) if spec.rss_url_env else None
        if not any((cookie, api_key, username, password, rss_url)):
            return None
        return SiteCredentials(
            cookie=SecretStr(cookie) if cookie else None,
            api_key=SecretStr(api_key) if api_key else None,
            username=SecretStr(username) if username else None,
            password=SecretStr(password) if password else None,
            rss_url=SecretStr(rss_url) if rss_url else None,
        )
