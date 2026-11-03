"""Profile 驱动的 NexusPHP 搜索与按需下载适配器。

改写自 MoviePilot `app/modules/indexer/spider/` 的公开适配边界，参考
`MoviePilot@04963c46080c2496f2df3fe5958f6a4eb75a3910`，GPL-3.0。
此实现不复制闭源站点资源中的具体站点规则；selectors 和请求参数来自显式 profile。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urljoin

from lxml import etree, html
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from pt_mcp.adapters.base import (
    AdapterError,
    AsyncHttpClient,
    CredentialMissing,
    CredentialProvider,
    ResourceRecord,
    SearchPage,
    SiteConfig,
    build_url,
    category_values,
    load_yaml_profile_data,
    make_resource_id,
    public_detail_url,
    require_credentials,
    response_location,
    response_ok,
    secret_value,
    validate_allowed_url,
)


class NexusSearchProfile(BaseModel):
    """声明单个 NexusPHP profile 的搜索路径、分页和 XPath 字段。"""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST"] = "GET"
    path: str = Field(min_length=1, max_length=2048)
    params: dict[str, str] = Field(default_factory=dict)
    body_encoding: Literal["form", "json"] = "form"
    category_separator: str = ","
    first_page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=500)
    container_xpath: str = Field(min_length=1, max_length=2048)
    empty_xpath: str = Field(min_length=1, max_length=2048)
    auth_required_xpath: str | None = Field(default=None, max_length=2048)
    captcha_xpath: str | None = Field(default=None, max_length=2048)
    item_xpath: str = Field(min_length=1, max_length=2048)
    has_more_xpath: str | None = Field(default=None, max_length=2048)
    fields: dict[str, str] = Field(min_length=3)
    labels_xpath: str | None = Field(default=None, max_length=2048)

    @field_validator("fields")
    @classmethod
    def require_minimum_fields(cls, fields: dict[str, str]) -> dict[str, str]:
        """确保 profile 能识别资源且保留其站内原生身份。"""
        required = {"resource_id", "title", "download_ref"}
        if not required.issubset(fields):
            raise PydanticCustomError(
                "required_resource_fields",
                "fields must define resource_id, title, and download_ref",
            )
        return fields


class NexusPHPProfile(BaseModel):
    """校验可随程序发布的 NexusPHP 搜索 profile。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    kind: Literal["nexusphp"]
    search: NexusSearchProfile

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> NexusPHPProfile:
        """从映射创建已校验的 profile。"""
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            raise AdapterError("NexusPHP profile 格式无效", code="profile_invalid") from error


def load_profile() -> NexusPHPProfile:
    """从安装包内读取默认 NexusPHP profile。"""
    return NexusPHPProfile.from_mapping(
        load_yaml_profile_data(None, profile_dir=Path("."), adapter_name="nexusphp")
    )


def load_profile_for_site(profile_ref: str | None, *, profile_dir: Path) -> NexusPHPProfile:
    """Load the built-in or confined per-site NexusPHP profile."""
    return NexusPHPProfile.from_mapping(
        load_yaml_profile_data(profile_ref, profile_dir=profile_dir, adapter_name="nexusphp")
    )


class NexusPHPAdapter:
    """使用显式站点 profile 的异步 NexusPHP 适配器。"""

    def __init__(
        self,
        http: AsyncHttpClient,
        credentials: CredentialProvider,
        *,
        profile: NexusPHPProfile | Mapping[str, Any],
    ) -> None:
        """保存共享 HTTP 客户端、凭据提供器和经过校验的站点规则。"""
        self._http = http
        self._credentials = credentials
        self._profile = profile if isinstance(profile, NexusPHPProfile) else NexusPHPProfile.from_mapping(profile)

    async def search_torrents(
        self,
        site: SiteConfig,
        *,
        keyword: str,
        category_ids: list[str] | None,
        page: int,
    ) -> SearchPage[ResourceRecord]:
        """按 profile 指定的请求参数、分类和字段 XPath 搜索一页。"""
        if site.kind != "nexusphp":
            raise AdapterError("NexusPHPAdapter 仅接受 kind=nexusphp 的站点", code="site_config_invalid")
        if page < 1:
            raise AdapterError("页码必须从 1 开始", code="request_invalid")
        credentials = await require_credentials(self._credentials, site)
        cookie = secret_value(credentials.cookie)
        if not cookie:
            raise CredentialMissing(f"站点 {site.site_id} 未配置 Cookie")

        search = self._profile.search
        category_list = [str(value) for value in category_ids or []]
        replacements = {
            "keyword": keyword,
            "category_ids": search.category_separator.join(category_list),
            "page": str(search.first_page + page - 1),
        }
        params: dict[str, str] = {}
        for name, template in search.params.items():
            if "{category_ids}" in template and not category_list:
                continue
            try:
                params[name] = template.format_map(replacements)
            except (KeyError, ValueError) as error:
                raise AdapterError("NexusPHP profile 包含不支持的请求占位符", code="profile_invalid") from error

        url = build_url(site.base_url, search.path)
        kwargs: dict[str, Any] = {
            "method": search.method,
            "url": url,
            "cookies": cookie,
            "timeout_s": site.timeout_s,
            "proxy": site.proxy,
            "follow_redirects": False,
            "site_id": site.site_id,
        }
        if search.method == "GET":
            kwargs["params"] = params
        elif search.body_encoding == "json":
            kwargs["json"] = params
        else:
            kwargs["headers"] = {"Content-Type": "application/x-www-form-urlencoded"}
            kwargs["content"] = urlencode(params).encode("utf-8")

        response = await self._http.request(**kwargs)
        if response.status_code in {401, 403}:
            raise AdapterError("site_auth_required: NexusPHP 需要重新认证", code="site_auth_required")
        response_ok(response, "NexusPHP 搜索")
        try:
            document = html.fromstring(response.body)
            auth_markers = document.xpath(search.auth_required_xpath) if search.auth_required_xpath else []
            captcha_markers = document.xpath(search.captcha_xpath) if search.captcha_xpath else []
            containers = document.xpath(search.container_xpath)
            items = document.xpath(search.item_xpath)
            more_nodes = document.xpath(search.has_more_xpath) if search.has_more_xpath else None
            empty_markers = document.xpath(search.empty_xpath) if search.empty_xpath else []
        except (TypeError, ValueError, etree.XPathError, etree.ParserError) as error:
            raise AdapterError("NexusPHP 搜索页面无法按 profile 解析", code="page_incompatible") from error

        if captcha_markers:
            raise AdapterError("NexusPHP 返回验证页面", code="captcha_required")
        if auth_markers:
            raise AdapterError("NexusPHP 需要重新认证", code="site_auth_required")
        if not containers:
            raise AdapterError("NexusPHP 列表容器未匹配", code="page_incompatible")
        if not items and not empty_markers:
            raise AdapterError("无法确认 NexusPHP 返回的是空列表", code="page_incompatible")

        records = [record for node in items if (record := self._parse_item(site, node, search)) is not None]
        if items and not records:
            raise AdapterError("NexusPHP 列表结构与字段配置不匹配", code="page_incompatible")
        return SearchPage(
            items=records,
            page=page,
            has_more=bool(more_nodes) if more_nodes is not None else None,
        )

    async def resolve_download(self, site: SiteConfig, item: ResourceRecord) -> bytes | str:
        """仅在下载操作中请求 profile 解析出的 torrent URL。"""
        self._validate_item_site(site, item)
        locator = item.locator.get_secret_value() if item.locator else None
        if not locator:
            raise AdapterError("资源没有可用的站点下载定位符", code="resource_not_downloadable")
        if locator.startswith("magnet:"):
            return locator

        credentials = await require_credentials(self._credentials, site)
        cookie = secret_value(credentials.cookie)
        if not cookie:
            raise CredentialMissing(f"站点 {site.site_id} 未配置 Cookie")
        url = validate_allowed_url(urljoin(site.base_url.rstrip("/") + "/", locator), site)
        for _ in range(6):
            response = await self._http.request(
                method="GET",
                url=url,
                cookies=cookie,
                timeout_s=site.timeout_s,
                proxy=site.proxy,
                follow_redirects=False,
                site_id=site.site_id,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response_location(response)
                if not location:
                    raise AdapterError("下载响应缺少跳转地址", code="download_redirect_invalid")
                url = validate_allowed_url(urljoin(url, location), site)
                continue
            response_ok(response, "NexusPHP 下载")
            return response.body
        raise AdapterError("下载跳转次数超过上限", code="download_redirect_limit")

    def _parse_item(self, site: SiteConfig, node: Any, profile: NexusSearchProfile) -> ResourceRecord | None:
        """读取 profile 字段并保留原生分类和站内资源 ID。"""
        fields = profile.fields
        raw_id = self._xpath_string(node, fields["resource_id"])
        title = self._xpath_string(node, fields["title"])
        raw_download = self._xpath_string(node, fields["download_ref"])
        if not raw_id or not title or not raw_download:
            return None

        native_ids = self._split_categories(
            self._xpath_string(node, fields["native_category"]) if fields.get("native_category") else None
        )
        categories = category_values(site, native_ids)
        size = self._optional_int(node, fields.get("size_bytes"))
        seeders = self._optional_int(node, fields.get("seeders"))
        leechers = self._optional_int(node, fields.get("leechers"))
        published_at = self._optional_datetime(node, fields.get("published_at"))
        detail = self._xpath_string(node, fields["detail_url"]) if fields.get("detail_url") else None
        description = self._xpath_string(node, fields["description"]) if fields.get("description") else None
        labels = self._xpath_values(node, profile.labels_xpath) if profile.labels_xpath else []
        download_url = validate_allowed_url(
            urljoin(site.base_url.rstrip("/") + "/", raw_download),
            site,
        )

        return ResourceRecord(
            site_id=site.site_id,
            resource_id=make_resource_id(site.site_id, raw_id),
            source_id=raw_id,
            public_source_id=raw_id,
            native_categories=categories,
            title=title,
            description=description or None,
            detail_url=public_detail_url(detail, base_url=site.base_url),
            size_bytes=size,
            seeders=seeders,
            peers=leechers,
            published_at=published_at,
            labels=labels,
            locator=SecretStr(download_url),
            config_version=site.config_version,
        )

    @staticmethod
    def _xpath_values(node: Any, expression: str) -> list[str]:
        """将 XPath 字符串或节点结果统一成非空文本列表。"""
        result = node.xpath(expression)
        values = result if isinstance(result, list) else [result]
        return [str(value).strip() for value in values if value is not None and str(value).strip()]

    @classmethod
    def _xpath_string(cls, node: Any, expression: str) -> str | None:
        """从 XPath 结果取得第一个非空文本值。"""
        values = cls._xpath_values(node, expression)
        return values[0] if values else None

    @classmethod
    def _optional_int(cls, node: Any, expression: str | None) -> int | None:
        """解析可缺省的整数，不把无效值伪装成零。"""
        raw = cls._xpath_string(node, expression) if expression else None
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    @classmethod
    def _optional_datetime(cls, node: Any, expression: str | None) -> datetime | None:
        """解析 ISO 8601 发布时间；不可解析时返回未知。"""
        raw = cls._xpath_string(node, expression) if expression else None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _split_categories(raw: str | None) -> list[str]:
        """按通用分隔符拆分原生分类 ID。"""
        if not raw:
            return []
        return [value for part in raw.replace(";", ",").split(",") if (value := part.strip())]

    @staticmethod
    def _validate_item_site(site: SiteConfig, item: ResourceRecord) -> None:
        """拒绝把一个站点的私有下载定位符交给另一个站点适配器。"""
        if item.site_id != site.site_id:
            raise AdapterError("资源与请求站点不匹配", code="resource_site_mismatch")


__all__ = [
    "NexusPHPAdapter",
    "NexusPHPProfile",
    "NexusSearchProfile",
    "load_profile",
    "load_profile_for_site",
]
