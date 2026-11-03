"""共享 PT 站点适配器合同与安全 URL 辅助函数。

改写自 MoviePilot 站点适配边界的独立实现，参考源码版本
`MoviePilot@04963c46080c2496f2df3fe5958f6a4eb75a3910`，GPL-3.0。
此模块不包含 MoviePilot 的站点资源规则或认证资源包代码。
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
import re
from typing import Protocol, TypeAlias
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from pydantic import SecretStr
import yaml

from pt_mcp.credentials import CredentialProvider, SiteCredentials
from pt_mcp.errors import AdapterError, CredentialMissing
from pt_mcp.http import AsyncHttpClient, HttpResponse
from pt_mcp.models import NativeCategory, ResourceRecord, SearchPage, SiteConfig, make_resource_id

HeaderMap: TypeAlias = dict[str, str]


class SiteAdapter(Protocol):
    """异步站点适配器的最小公开合同。"""

    async def search_torrents(
        self,
        site: SiteConfig,
        *,
        keyword: str,
        category_ids: list[str] | None,
        page: int,
    ) -> SearchPage[ResourceRecord]:
        """按站点原生分类搜索一页资源。"""

    async def resolve_download(self, site: SiteConfig, item: ResourceRecord) -> bytes | str:
        """仅在调用下载操作时解析当前资源的下载数据或 magnet。"""


async def require_credentials(
    provider: CredentialProvider,
    site: SiteConfig,
) -> SiteCredentials:
    """按引用取站点凭据；发现/配置阶段不读取或缓存秘密值。"""
    if not site.credential_ref:
        raise CredentialMissing(f"站点 {site.site_id} 未配置凭据引用")
    credentials = await provider.get(site.credential_ref)
    if credentials is None:
        raise CredentialMissing(f"站点 {site.site_id} 的凭据引用不可用")
    return credentials


def secret_value(value: SecretStr | None) -> str | None:
    """从 SecretStr 取仅供当前请求使用的明文。"""
    return value.get_secret_value() if value is not None else None


def category_values(site: SiteConfig, category_ids: Iterable[str] | None) -> list[NativeCategory]:
    """将原生类别 ID 映射到站点配置的标签，保留未知 ID。"""
    labels = {item.id: item.label for item in site.native_categories}
    result: list[NativeCategory] = []
    seen: set[str] = set()
    for raw_id in category_ids or ():
        category_id = str(raw_id).strip()
        if category_id and category_id not in seen:
            result.append(NativeCategory(id=category_id, label=labels.get(category_id, category_id)))
            seen.add(category_id)
    return result


def build_url(base_url: str, path: str) -> str:
    """以配置的站点基址解析 profile 中的相对路径。"""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise AdapterError("站点基址必须是无用户信息的 HTTP(S) URL", code="site_config_invalid")
    candidate = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    validate_allowed_url(candidate, (parsed.hostname,))
    return candidate


def allowed_hosts(site: SiteConfig) -> frozenset[str]:
    """返回站点允许访问的精确主机集合。"""
    candidates = [site.base_url]
    if site.api_base_url:
        candidates.append(site.api_base_url)
    candidates.extend(site.download_hosts)
    hosts: set[str] = set()
    for candidate in candidates:
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.hostname:
            hosts.add(parsed.hostname.lower().rstrip("."))
    return frozenset(hosts)


def validate_allowed_url(url: str, allowed: Iterable[str] | SiteConfig) -> str:
    """限制外部 HTTP 请求只能访问站点显式允许的主机。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AdapterError("站点返回了无效的 HTTP(S) 地址", code="download_url_invalid")
    if parsed.username or parsed.password:
        raise AdapterError("站点返回地址包含 URL 用户信息", code="download_url_invalid")
    hosts = allowed_hosts(allowed) if isinstance(allowed, SiteConfig) else {
        value.lower().rstrip(".") for value in allowed
    }
    if parsed.hostname.lower().rstrip(".") not in hosts:
        raise AdapterError("站点返回地址的主机不在允许列表中", code="download_host_blocked")
    return url


_SENSITIVE_QUERY_KEYS = frozenset({
    "apikey", "api_key", "auth", "authorization", "credential", "key",
    "passkey", "password", "secret", "sig", "signature", "ticket", "token",
    "access_token", "refresh_token", "download_token", "expires", "expiration",
})


def is_sensitive_query_key(key: str) -> bool:
    """Recognize common authentication and short-lived URL parameter names."""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in {re.sub(r"[^a-z0-9]", "", value) for value in _SENSITIVE_QUERY_KEYS}


def public_detail_url(url: str | None, *, base_url: str) -> str | None:
    """只公开无认证参数的详情 URL。"""
    if not url:
        return None
    absolute = urljoin(base_url.rstrip("/") + "/", url)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if any(is_sensitive_query_key(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return None
    if any(is_sensitive_query_key(segment) for segment in parsed.path.split("/") if segment):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(parse_qsl(parsed.query, keep_blank_values=True)), ""))


def response_location(response: HttpResponse) -> str | None:
    """不区分大小写地读取跳转地址。"""
    for name, value in response.headers.items():
        if name.lower() == "location":
            return value
    return None


def response_json(response: HttpResponse) -> object:
    """将 JSON 响应解码为数据；错误信息不包含响应正文。"""
    import json

    try:
        return json.loads(response.text)
    except (TypeError, ValueError) as error:
        raise AdapterError("站点返回了无效 JSON", code="response_invalid") from error


def response_ok(response: HttpResponse, operation: str) -> None:
    """把非成功状态转换为不带正文或 URL 的适配器错误。"""
    if not 200 <= response.status_code < 300:
        raise AdapterError(f"{operation}失败，HTTP {response.status_code}", code="http_error")


def load_yaml_profile_data(
    profile_ref: str | None,
    *,
    profile_dir: Path,
    adapter_name: str,
) -> dict[str, object]:
    """Load an adapter-owned package profile or a locally configured YAML profile."""
    if profile_ref is None or profile_ref == adapter_name:
        try:
            content = files("pt_mcp").joinpath(f"resources/profiles/{adapter_name}.yaml").read_text(
                encoding="utf-8"
            )
        except (OSError, ModuleNotFoundError):
            raise AdapterError("无法读取内置站点 profile", code="profile_unavailable") from None
    else:
        root_path = Path(profile_dir).expanduser()
        try:
            root = root_path.resolve()
            candidate = Path(profile_ref).expanduser()
            resolved = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
            if not resolved.is_file() or resolved.suffix.lower() not in {".yaml", ".yml"}:
                raise AdapterError("profile_ref 必须指向 YAML profile 文件", code="profile_invalid")
            content = resolved.read_text(encoding="utf-8")
        except AdapterError:
            raise
        except (OSError, ValueError):
            raise AdapterError("自定义站点 profile 不存在或无法读取", code="profile_unavailable") from None

    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError:
        raise AdapterError("站点 profile YAML 无效", code="profile_invalid") from None
    if not isinstance(payload, dict):
        raise AdapterError("站点 profile 根节点必须是对象", code="profile_invalid")
    return payload


__all__ = [
    "AdapterError",
    "AsyncHttpClient",
    "CredentialMissing",
    "CredentialProvider",
    "HeaderMap",
    "HttpResponse",
    "NativeCategory",
    "ResourceRecord",
    "SearchPage",
    "SiteAdapter",
    "SiteConfig",
    "SiteCredentials",
    "allowed_hosts",
    "build_url",
    "category_values",
    "public_detail_url",
    "require_credentials",
    "response_json",
    "response_location",
    "response_ok",
    "secret_value",
    "validate_allowed_url",
    "make_resource_id",
    "is_sensitive_query_key",
    "load_yaml_profile_data",
]
