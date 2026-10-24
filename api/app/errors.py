import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

_DEFAULT_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    429: "rate_limited",
}


class ApiError(StarletteHTTPException):
    """Raise ApiError(status, code, message) for every non-2xx response."""

    def __init__(self, status_code: int, code: str, message: str, headers=None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        code = getattr(exc, "code", None) or _DEFAULT_CODES.get(exc.status_code, "http_error")
        return JSONResponse(
            error_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid')}" if where else "Invalid request"
        return JSONResponse(error_body("validation_error", message), status_code=422)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(_: Request, exc: RateLimitExceeded):
        return JSONResponse(error_body("rate_limited", f"Rate limit exceeded: {exc.detail}"), 429)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(error_body("internal_error", "Internal server error"), 500)
