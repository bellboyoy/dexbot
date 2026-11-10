class ProviderError(Exception):
    """Base class for market data provider failures."""

    def __init__(self, provider: str, message: str):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


class ProviderUnavailable(ProviderError):
    """Provider unreachable, erroring, or its circuit breaker is open."""


class RateLimited(ProviderUnavailable):
    """Provider kept returning 429 after retries."""


class InvalidResponse(ProviderError):
    """Response could not be parsed into the expected shape."""


class CapabilityNotSupported(ProviderError):
    """The provider does not offer this kind of data."""


class UnsupportedPoolId(ProviderError):
    """The provider cannot query this pool id. Counted separately from an invalid address."""

    reason = "unsupported_pool_id"
