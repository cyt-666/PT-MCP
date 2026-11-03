"""Stable, credential-safe errors shared by service and adapters."""

from __future__ import annotations


class AppError(Exception):
    """An expected error with a safe code and public message."""

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


class CredentialMissing(AppError):
    """A configured capability was invoked without its required credential."""

    def __init__(self, public_message: str = "该操作缺少站点凭据") -> None:
        super().__init__("credential_missing", public_message)


class NotFound(AppError):
    """A configured site, resource, downloader, or receipt was not found."""

    def __init__(self, public_message: str = "请求的资源不存在") -> None:
        super().__init__("not_found", public_message)


class IdempotencyConflict(AppError):
    """A request ID was reused for a different operation payload."""

    def __init__(self) -> None:
        super().__init__("idempotency_conflict", "request_id 已用于不同的下载请求")


class SubmissionTransportUnknown(AppError):
    """A downloader add request may have reached qBittorrent, so it must not replay."""

    def __init__(self) -> None:
        super().__init__("submission_unknown", "下载器提交结果未知；请先查询任务状态")


class AdapterError(AppError):
    """A site adapter failure with no raw response, URL, or exception text."""

    def __init__(self, public_message: str, *, code: str = "adapter_error") -> None:
        super().__init__(code, public_message)


class HttpError(AppError):
    """A transport failure without exception details or request URL."""

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(code, public_message)


class DownloaderError(AppError):
    """A safe downloader failure classification."""

    def __init__(self, public_message: str, *, code: str = "downloader_error") -> None:
        super().__init__(code, public_message)


class ConfigError(AppError):
    """A missing or malformed local configuration file."""

    def __init__(self, public_message: str) -> None:
        super().__init__("config_error", public_message)
