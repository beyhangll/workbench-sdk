"""Errors of the SDK. Every message says what happened, and most say what to do next."""
from __future__ import annotations


class AQError(Exception):
    """A problem with a plain message and, when there is one, a fix."""

    def __init__(self, message: str, fix: str | None = None, *, status: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.status = status
        self.code = code

    def __str__(self) -> str:
        return f"{self.message} {self.fix}" if self.fix else self.message


class RegressionError(AQError):
    """The run is a regression and the caller asked to fail on one."""
