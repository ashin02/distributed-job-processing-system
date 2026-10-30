"""Developer bearer authentication and isolated anonymous demo sessions."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from hmac import new as new_hmac
from secrets import token_bytes
from time import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from computegrid_api.config import Settings, get_settings

bearer_scheme = HTTPBearer(auto_error=False)
DEMO_COOKIE_NAME = "computegrid_demo_session"


@dataclass(frozen=True, slots=True)
class AuthenticatedClient:
    """Server-derived identity and authorization class for one caller."""

    client_id: str
    is_developer: bool = False


def _encode_demo_session(settings: Settings) -> tuple[str, str]:
    session_id = urlsafe_b64encode(token_bytes(24)).decode("ascii").rstrip("=")
    expires_at = int(time()) + settings.demo_session_ttl_seconds
    payload = f"v1.{session_id}.{expires_at}"
    signature = new_hmac(
        settings.demo_session_secret.get_secret_value().encode(),
        payload.encode(),
        sha256,
    ).digest()
    token = f"{payload}.{urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"
    client_id = f"demo-{sha256(session_id.encode()).hexdigest()}"
    return token, client_id


def _decode_demo_session(token: str, settings: Settings) -> str | None:
    try:
        version, session_id, expires_text, signature_text = token.split(".")
        expires_at = int(expires_text)
        if len(session_id) != 32 or len(signature_text) != 43:
            return None
        signature = urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
    except (BinasciiError, ValueError, TypeError):
        return None
    if version != "v1" or expires_at < int(time()):
        return None
    payload = f"{version}.{session_id}.{expires_at}"
    expected = new_hmac(
        settings.demo_session_secret.get_secret_value().encode(), payload.encode(), sha256
    ).digest()
    if not compare_digest(signature, expected):
        return None
    return f"demo-{sha256(session_id.encode()).hexdigest()}"


def authenticate_client(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
    request: Request,
    response: Response,
) -> AuthenticatedClient:
    """Authenticate a developer or establish a signed anonymous browser session."""
    expected_token = settings.development_api_token.get_secret_value()
    if credentials is not None:
        if credentials.scheme.lower() == "bearer" and compare_digest(
            credentials.credentials, expected_token
        ):
            return AuthenticatedClient(
                client_id=settings.development_client_id,
                is_developer=True,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not settings.demo_mode_enabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    cookie = request.cookies.get(DEMO_COOKIE_NAME)
    client_id = _decode_demo_session(cookie, settings) if cookie else None
    if client_id is None:
        cookie, client_id = _encode_demo_session(settings)
        response.set_cookie(
            key=DEMO_COOKIE_NAME,
            value=cookie,
            max_age=settings.demo_session_ttl_seconds,
            httponly=True,
            secure=settings.demo_cookie_secure,
            samesite=settings.demo_cookie_samesite,
            path="/",
        )
    return AuthenticatedClient(client_id=client_id, is_developer=False)
