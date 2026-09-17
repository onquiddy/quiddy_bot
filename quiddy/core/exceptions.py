class QuiddyError(Exception):
    """Base exception for the Quiddy platform."""


class ConfigurationError(QuiddyError):
    pass


class ServiceError(QuiddyError):
    pass


class PluginError(QuiddyError):
    pass


class PluginDependencyError(PluginError):
    pass


class PluginLoadError(PluginError):
    pass


class APIError(QuiddyError):
    """Base error raised by the Quiddy Internal API client."""

    def __init__(self, message: str, *, status: int | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class APIConnectionError(APIError):
    pass


class APIAuthenticationError(APIError):
    pass


class APIRateLimitError(APIError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
