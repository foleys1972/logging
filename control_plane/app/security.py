"""Simple API token authentication utilities."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings


_bearer = HTTPBearer(auto_error=False)


def require_agent_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    _verify_token(credentials, settings.agent_api_tokens)


def require_dashboard_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    _verify_token(credentials, settings.dashboard_api_tokens)


def _verify_token(
    credentials: HTTPAuthorizationCredentials | None,
    allowed_tokens: list[str],
) -> None:
    if not allowed_tokens:
        # If no tokens configured, allow all requests (development mode)
        return

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

    token = credentials.credentials
    if token not in allowed_tokens:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")


