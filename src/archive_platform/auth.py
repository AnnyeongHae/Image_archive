from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


@dataclass(frozen=True)
class AdminPrincipal:
    subject: str
    email: str | None
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"missing_scope:{scope}")


def _csv_env(name: str) -> set[str]:
    return {item.strip().lower() for item in os.environ.get(name, "").split(",") if item.strip()}


def _normalized_team_domain(value: str) -> str:
    raw = value.strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".cloudflareaccess.com"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="cloudflare_access_team_domain_invalid")
    return raw


@lru_cache(maxsize=4)
def _jwk_client(certs_url: str):
    import jwt

    return jwt.PyJWKClient(certs_url, cache_keys=True, lifespan=300)


def _cloudflare_principal(token: str) -> AdminPrincipal:
    import jwt

    team_domain = _normalized_team_domain(os.environ.get("CF_ACCESS_TEAM_DOMAIN", ""))
    audience = os.environ.get("CF_ACCESS_AUD", "").strip()
    allowed_emails = _csv_env("ARCHIVE_ADMIN_EMAILS")
    if not audience or not allowed_emails:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="cloudflare_access_auth_not_configured")
    try:
        signing_key = _jwk_client(f"{team_domain}/cdn-cgi/access/certs").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=team_domain,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cloudflare_access_token_invalid") from exc
    email = str(claims.get("email") or "").lower()
    if not email or email not in allowed_emails:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_email_not_allowed")
    scopes = {"archive:read", "review:write"}
    if email in _csv_env("ARCHIVE_QUARANTINE_EMAILS"):
        scopes.add("quarantine:read")
    return AdminPrincipal(subject=str(claims["sub"]), email=email, scopes=frozenset(scopes))


def _local_principal(request: Request) -> AdminPrincipal:
    if os.environ.get("ARCHIVE_RUNTIME", "").lower() != "development":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="local_auth_requires_development_runtime")
    host = request.client.host if request.client else ""
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local_auth_loopback_only")
    expected = os.environ.get("ARCHIVE_LOCAL_ADMIN_TOKEN", "")
    supplied = request.headers.get("X-Archive-Admin-Token", "")
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="local_admin_token_not_configured")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local_admin_token_invalid")
    scopes = _csv_env("ARCHIVE_LOCAL_ADMIN_SCOPES") or {"archive:read", "review:write"}
    return AdminPrincipal(subject="local-admin", email=None, scopes=frozenset(scopes))


def authenticate_admin(request: Request) -> AdminPrincipal:
    """Fail-closed administrator authentication.

    A Cloudflare header is never trusted by itself: its JWT signature, issuer,
    audience, lifetime, and allowlisted identity are verified.  Local tokens
    work only in an explicitly declared development runtime on loopback.
    """

    mode = os.environ.get("ARCHIVE_ADMIN_AUTH_MODE", "disabled").strip().lower()
    if mode == "cloudflare_access":
        token = request.headers.get("Cf-Access-Jwt-Assertion", "")
        if not token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cloudflare_access_token_missing")
        return _cloudflare_principal(token)
    if mode == "local_token":
        return _local_principal(request)
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="admin_auth_disabled")
