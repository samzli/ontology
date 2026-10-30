"""Common custom exceptions used across the Onti platform.

These are small subclasses that carry an HTTP status code so the FastAPI
exception handlers in :mod:`main` can translate them into clean JSON without
duplicating the status decision in every route.
"""

from __future__ import annotations


class UploadError(Exception):
    """Upload / ingestion failure that carries an HTTP status for the web layer.

    Attributes:
        message (str): Human-readable reason shown to the caller.
        status_code (int): HTTP status the API should return (default 400).
    """

    def __init__(self, message: str, status_code: int = 400):
        """Build the error with a user-facing message and HTTP status.

        Args:
            message (str): Human-readable reason shown to the caller.
            status_code (int): HTTP status the API should return (default 400).
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
