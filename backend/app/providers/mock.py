"""Side-effect-free mock provider: no network, no money, no randomness."""

from app.providers.base import ChargeRequest, ChargeResult, ProviderUnavailableError


class MockPaymentProvider:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    def charge(self, request: ChargeRequest) -> ChargeResult:
        if self._fail:
            raise ProviderUnavailableError("simulated provider outage")
        # Deterministic reference derived from the key digest.
        return ChargeResult(provider_reference=f"mock_{request.idempotency_key_digest[:16]}")
