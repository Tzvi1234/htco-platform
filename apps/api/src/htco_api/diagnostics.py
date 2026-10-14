"""Diagnostic endpoints for proving monitoring end to end. Never enabled in production."""

from fastapi import APIRouter


class DiagnosticError(RuntimeError):
    """Raised on purpose by `/_diagnostics/error`."""


def diagnostics_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.post("/_diagnostics/error")
    async def raise_diagnostic_error() -> None:
        """Fail on purpose, so an operator can see the error reach monitoring."""
        raise DiagnosticError("Deliberate error from /_diagnostics/error")

    return router
