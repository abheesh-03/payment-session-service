"""Shared API errors and their consistent JSON representation.

Every error response has the shape::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_id import REQUEST_ID_HEADER, get_request_id

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"
    message = "An unexpected error occurred."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message


class SessionNotFoundError(AppError):
    status_code = 404
    code = "SESSION_NOT_FOUND"
    message = "Payment session not found."


class SessionAlreadyPaidError(AppError):
    status_code = 409
    code = "SESSION_ALREADY_PAID"
    message = "Payment session has already been paid."


class SessionExpiredError(AppError):
    status_code = 409
    code = "SESSION_EXPIRED"
    message = "Payment session has expired."


class IdempotencyConflictError(AppError):
    status_code = 409
    code = "IDEMPOTENCY_CONFLICT"
    message = "Idempotency-Key was already used for a different request."


class InvalidRequestError(AppError):
    status_code = 422
    code = "INVALID_REQUEST"
    message = "Invalid request."


class PaymentProviderUnavailableError(AppError):
    status_code = 503
    code = "PAYMENT_PROVIDER_UNAVAILABLE"
    message = (
        "The payment provider is temporarily unavailable. No payment was made; "
        "retry later with the same Idempotency-Key."
    )


class PaymentProviderError(AppError):
    """Unexpected provider failure; payment work was rolled back."""

    status_code = 500
    code = "INTERNAL_ERROR"
    message = AppError.message


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


ERROR_RESPONSES = {
    500: {"model": ErrorResponse, "description": "Unexpected error (code INTERNAL_ERROR)"},
    503: {
        "model": ErrorResponse,
        "description": "Payment provider unavailable (code PAYMENT_PROVIDER_UNAVAILABLE)",
    },
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {
        "model": ErrorResponse,
        "description": (
            "SESSION_ALREADY_PAID, SESSION_EXPIRED, or IDEMPOTENCY_CONFLICT"
        ),
    },
    422: {"model": ErrorResponse, "description": "Invalid request (code INVALID_REQUEST)"},
}

_HTTP_ERROR_CODES = {
    400: "INVALID_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = get_request_id(request)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        # Set explicitly: 500s are rendered outside the request-ID middleware.
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


def _describe_validation_errors(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "path", "query", "header")]
        field = ".".join(loc) or "request body"
        parts.append(f"{field}: {err.get('msg', 'invalid value')}")
    return "; ".join(parts) or "Invalid request."


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return error_response(request, exc.status_code, exc.code, exc.message)


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(request, 422, "INVALID_REQUEST", _describe_validation_errors(exc))


async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return error_response(request, exc.status_code, code, message, headers=exc.headers)


async def _handle_database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    # Never surface the driver message: it can include hosts and usernames.
    logger.error("Database operation failed: %s", type(exc.orig).__name__)
    return error_response(
        request, 503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable."
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error: %s", type(exc).__name__)
    return error_response(request, 500, AppError.code, AppError.message)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(OperationalError, _handle_database_unavailable)
    app.add_exception_handler(Exception, _handle_unexpected_error)
