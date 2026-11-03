"""显式 API profile 驱动的 M-Team 适配器。

改写自 MoviePilot M-Team adapter 的公开 API 请求边界，参考
`MoviePilot@04963c46080c2496f2df3fe5958f6a4eb75a3910`，GPL-3.0。
不包含闭源站点资源规则；API host、凭据和原生分类均由调用方配置。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pt_mcp.adapters.base import (
    AdapterError,
    AsyncHttpClient,
    CredentialMissing,
    CredentialProvider,
    ResourceRecord,
    SearchPage,
    SiteConfig,
    allowed_hosts,
    build_url,
    category_values,
    load_yaml_profile_data,
    make_resource_id,
    public_detail_url,
    require_credentials,
    response_json,
    response_location,
    response_ok,
    secret_value,
    validate_allowed_url,
)


class MTeamSearchProfile(BaseModel):
    """声明 M-Team 搜索 API 的路径和分页参数。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=2048)
    page_size: int = Field(default=50, ge=1, le=500)
    first_page: int = Field(default=1, ge=1)


class MTeamDownloadTokenProfile(BaseModel):
    """声明 M-Team 按需生成短时下载地址的 API 路径。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=2048)
    resource_id_field: str = Field(default="id", min_length=1, max_length=64)


class MTeamProfile(BaseModel):
    """校验可随程序发布的 M-Team API profile。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    kind: Literal["mteam"]
    search: MTeamSearchProfile
    download_token: MTeamDownloadTokenProfile

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> MTeamProfile:
        """从映射创建已校验的 profile。"""
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            raise AdapterError("M-Team profile 格式无效", code="profile_invalid") from error


def load_profile() -> MTeamProfile:
    """从安装包内读取默认 M-Team profile。"""
    return MTeamProfile.from_mapping(
        load_yaml_profile_data(None, profile_dir=Path("."), adapter_name="mteam")
    )


def load_profile_for_site(profile_ref: str | None, *, profile_dir: Path) -> MTeamProfile:
    """Load the built-in or confined per-site M-Team profile."""
    return MTeamProfile.from_mapping(
        load_yaml_profile_data(profile_ref, profile_dir=profile_dir, adapter_name="mteam")
    )


class MTeamAdapter:
    """通过 API Key 搜索并在下载操作时换取短时 torrent 数据。"""

    def __init__(
        self,
        http: AsyncHttpClient,
        credentials: CredentialProvider,
        *,
        profile: MTeamProfile | Mapping[str, Any],
    ) -> None:
        """保存共享 HTTP 客户端、凭据提供器和经过校验的 API profile。"""
        self._http = http
        self._credentials = credentials
        self._profile = profile if isinstance(profile, MTeamProfile) else MTeamProfile.from_mapping(profile)

    async def search_torrents(
        self,
        site: SiteConfig,
        *,
        keyword: str,
        category_ids: list[str] | None,
        page: int,
    ) -> SearchPage[ResourceRecord]:
        """按站点原生分类调用搜索 API，不请求种子下载地址。"""
        if site.kind != "mteam":
            raise AdapterError("MTeamAdapter 仅接受 kind=mteam 的站点", code="site_config_invalid")
        if not site.api_base_url:
            raise AdapterError("M-Team 站点必须显式配置 api_base_url", code="site_config_invalid")
        if page < 1:
            raise AdapterError("页码必须从 1 开始", code="request_invalid")
        credentials = await require_credentials(self._credentials, site)
        api_key = secret_value(credentials.api_key)
        if not api_key:
            raise CredentialMissing(f"站点 {site.site_id} 未配置 API Key")

        search_url = build_url(site.api_base_url, self._profile.search.path)
        payload = {
            "keyword": keyword,
            "categories": [str(value) for value in category_ids or []],
            "pageNumber": self._profile.search.first_page + page - 1,
            "pageSize": self._profile.search.page_size,
            "visible": 1,
        }
        response = await self._http.request(
            method="POST",
            url=search_url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            json=payload,
            timeout_s=site.timeout_s,
            proxy=site.proxy,
            follow_redirects=False,
            site_id=site.site_id,
        )
        if response.status_code in {401, 403}:
            raise AdapterError("M-Team API Key 未获授权", code="site_auth_required")
        response_ok(response, "M-Team 搜索")
        result = response_json(response)
        if not isinstance(result, dict):
            raise AdapterError("M-Team 搜索响应结构无效", code="page_incompatible")
        if result.get("code") not in (None, 0, "0"):
            raise AdapterError("M-Team 搜索接口返回失败状态", code="site_request_failed")
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise AdapterError("M-Team 搜索响应缺少资源列表", code="page_incompatible")

        if any(not isinstance(row, dict) for row in data["data"]):
            raise AdapterError("M-Team 搜索结果包含无效条目", code="page_incompatible")
        records = [self._parse_result(site, row) for row in data["data"]]
        current_page = data.get("pageNumber")
        total_pages = data.get("totalPages")
        has_more = (
            current_page < total_pages
            if isinstance(current_page, int) and isinstance(total_pages, int)
            else None
        )
        return SearchPage(items=records, page=page, has_more=has_more)

    async def resolve_download(self, site: SiteConfig, item: ResourceRecord) -> bytes | str:
        """仅在下载操作时生成 token，并将允许主机上的结果取回为 torrent bytes。"""
        if item.site_id != site.site_id:
            raise AdapterError("资源与请求站点不匹配")
        if not site.api_base_url:
            raise AdapterError("M-Team 站点必须显式配置 api_base_url", code="site_config_invalid")
        credentials = await require_credentials(self._credentials, site)
        api_key = secret_value(credentials.api_key)
        if not api_key:
            raise CredentialMissing(f"站点 {site.site_id} 未配置 API Key")

        token_url = build_url(site.api_base_url, self._profile.download_token.path)
        token_response = await self._http.request(
            method="POST",
            url=token_url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            json={self._profile.download_token.resource_id_field: item.source_id},
            timeout_s=site.timeout_s,
            proxy=site.proxy,
            follow_redirects=False,
            site_id=site.site_id,
        )
        if token_response.status_code in {401, 403}:
            raise AdapterError("M-Team 下载凭据未获授权", code="site_auth_required")
        response_ok(token_response, "M-Team 下载凭据生成")
        result = response_json(token_response)
        if not isinstance(result, dict):
            raise AdapterError("M-Team 下载凭据响应结构无效", code="download_resolution_failed")
        if result.get("code") not in (None, 0, "0"):
            raise AdapterError("M-Team 下载凭据生成失败", code="download_resolution_failed")
        download_target = result.get("data")
        if not isinstance(download_target, str) or not download_target:
            raise AdapterError("M-Team 下载凭据响应缺少下载地址", code="download_resolution_failed")
        if download_target.startswith("magnet:"):
            return download_target

        allowed = allowed_hosts(site)
        url = validate_allowed_url(
            urljoin(site.api_base_url.rstrip("/") + "/", download_target),
            allowed,
        )
        for _ in range(6):
            response = await self._http.request(
                method="GET",
                url=url,
                timeout_s=site.timeout_s,
                proxy=site.proxy,
                follow_redirects=False,
                site_id=site.site_id,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response_location(response)
                if not location:
                    raise AdapterError("M-Team 下载响应缺少跳转地址", code="download_redirect_invalid")
                url = validate_allowed_url(urljoin(url, location), allowed)
                continue
            response_ok(response, "M-Team 下载")
            return response.body
        raise AdapterError("M-Team 下载跳转次数超过上限", code="download_redirect_limit")

    @staticmethod
    def _parse_result(site: SiteConfig, row: Mapping[str, Any]) -> ResourceRecord:
        """投影 API 结果，保持站点资源 ID 和原生分类原样。"""
        resource_id = row.get("id")
        if resource_id is None or not str(resource_id).strip():
            raise AdapterError("M-Team 资源缺少站点 ID", code="page_incompatible")
        category_id = row.get("category")
        categories = category_values(site, [str(category_id)] if category_id is not None else None)
        status = row.get("status") if isinstance(row.get("status"), dict) else {}
        labels_value = row.get("labelsNew")
        labels = [str(value) for value in labels_value if value is not None] if isinstance(labels_value, list) else []
        detail_url = row.get("detail_url") if isinstance(row.get("detail_url"), str) else None
        return ResourceRecord(
            site_id=site.site_id,
            resource_id=make_resource_id(site.site_id, str(resource_id)),
            source_id=str(resource_id),
            public_source_id=str(resource_id),
            native_categories=categories,
            title=_required_text(row.get("name")),
            description=(str(row["smallDescr"]) if row.get("smallDescr") is not None else None),
            detail_url=public_detail_url(detail_url, base_url=site.base_url),
            size_bytes=_optional_int(row.get("size")),
            seeders=_optional_int(status.get("seeders")),
            peers=_optional_int(status.get("leechers")),
            published_at=_optional_datetime(row.get("createdDate")),
            labels=labels,
            locator=None,
            config_version=site.config_version,
        )


def _optional_int(value: object) -> int | None:
    """解析可缺省的整数，不把未知值伪装成零。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_text(value: object) -> str:
    """读取必需标题，避免把损坏响应当作有效空标题。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        raise AdapterError("M-Team 资源缺少标题", code="page_incompatible")
    return text


def _optional_datetime(value: object) -> datetime | None:
    """解析 ISO 8601 时间；其余格式保持未知。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "MTeamAdapter",
    "MTeamProfile",
    "MTeamSearchProfile",
    "load_profile",
    "load_profile_for_site",
]
