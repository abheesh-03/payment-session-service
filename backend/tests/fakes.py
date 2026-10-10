"""Test doubles for the payment provider."""

import threading
from collections.abc import Callable
from typing import Literal

from app.providers.base import ChargeRequest, ChargeResult, ProviderUnavailableError

# Looks like a secret on purpose: it must never reach responses or logs.
LEAKY_ERROR_TEXT = "provider exploded: postgresql://svc:hunter2@db.internal/payments"


class CountingProvider:
    """Records every charge; behavior can be switched between calls."""

    def __init__(
        self,
        behavior: Literal["success", "unavailable", "crash"] = "success",
        on_charge: Callable[[ChargeRequest], None] | None = None,
    ) -> None:
        self.behavior = behavior
        self.on_charge = on_charge
        self.calls: list[ChargeRequest] = []
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    def charge(self, request: ChargeRequest) -> ChargeResult:
        with self._lock:
            self.calls.append(request)
        if self.on_charge is not None:
            self.on_charge(request)
        if self.behavior == "unavailable":
            raise ProviderUnavailableError("simulated outage")
        if self.behavior == "crash":
            raise RuntimeError(LEAKY_ERROR_TEXT)
        return ChargeResult(provider_reference="counting_ref")
