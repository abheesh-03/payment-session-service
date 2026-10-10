"""Payment session HTTP endpoints."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.cache.receipt_cache import ReceiptCache
from app.core.config import Settings, get_settings
from app.core.errors import ERROR_RESPONSES, InvalidRequestError
from app.core.request_id import get_request_id
from app.db.database import get_db
from app.providers.base import PaymentProvider
from app.providers.mock import MockPaymentProvider
from app.schemas.payment_session import CreatePaymentSessionRequest, PaymentSessionResponse
from app.services.idempotency import IDEMPOTENCY_KEY_HEADER, IDEMPOTENCY_KEY_PATTERN
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_receipt_cache(request: Request, settings: SettingsDep) -> ReceiptCache | None:
    return ReceiptCache(
        request.app.state.redis,
        prefix=settings.redis_key_prefix,
        ttl_seconds=settings.receipt_cache_ttl_seconds,
    )


def get_payment_service(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    receipt_cache: Annotated[ReceiptCache | None, Depends(get_receipt_cache)],
) -> PaymentService:
    return PaymentService(db, request_id=get_request_id(request), receipt_cache=receipt_cache)


ServiceDep = Annotated[PaymentService, Depends(get_payment_service)]

MOCK_OUTCOME_HEADER = "X-Mock-Provider-Outcome"


def get_payment_provider(
    settings: SettingsDep,
    outcome: Annotated[Literal["success", "fail"], Header(alias=MOCK_OUTCOME_HEADER)] = "success",
) -> PaymentProvider:
    """The mock provider. The outcome header is a simulation control only:
    it is not part of the idempotency fingerprint."""
    if outcome == "fail" and not settings.enable_mock_failures:
        raise InvalidRequestError(
            f"{MOCK_OUTCOME_HEADER}: failure simulation is disabled (ENABLE_MOCK_FAILURES=false)"
        )
    return MockPaymentProvider(fail=outcome == "fail")


ProviderDep = Annotated[PaymentProvider, Depends(get_payment_provider)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PaymentSessionResponse,
    responses={422: ERROR_RESPONSES[422]},
)
def create_session(
    body: CreatePaymentSessionRequest, service: ServiceDep, settings: SettingsDep
) -> PaymentSessionResponse:
    payment_session = service.create_session(body)
    return PaymentSessionResponse.from_model(payment_session, settings.frontend_base_url)


@router.get(
    "/{session_id}",
    response_model=PaymentSessionResponse,
    responses={404: ERROR_RESPONSES[404], 422: ERROR_RESPONSES[422]},
)
def get_session(
    session_id: uuid.UUID, service: ServiceDep, settings: SettingsDep
) -> PaymentSessionResponse:
    payment_session = service.get_session(session_id)
    return PaymentSessionResponse.from_model(payment_session, settings.frontend_base_url)


@router.post(
    "/{session_id}/pay",
    response_model=PaymentSessionResponse,
    responses={
        code: ERROR_RESPONSES[code] for code in (404, 409, 422, 500, 503)
    },
)
def pay_session(
    session_id: uuid.UUID,
    idempotency_key: Annotated[
        str, Header(alias=IDEMPOTENCY_KEY_HEADER, pattern=IDEMPOTENCY_KEY_PATTERN)
    ],
    service: ServiceDep,
    provider: ProviderDep,
    settings: SettingsDep,
) -> JSONResponse:
    """Simulated payment completion. Takes no body: the target status and
    timestamps are decided by the server. Retries with the same
    Idempotency-Key replay the original successful response."""
    result = service.pay_session(
        session_id, idempotency_key, provider, settings.frontend_base_url
    )
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)
