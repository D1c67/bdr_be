"""Supabase JWT verification.

Supabase issues end-user JWTs. Newer projects sign with asymmetric keys (RS256/
ES256) discoverable via JWKS; legacy projects use a shared HS256 secret. We try
JWKS first and fall back to the shared secret if configured.
"""

from typing import Any

import jwt
from jwt import PyJWKClient

from app.core.config import get_settings

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(get_settings().supabase_jwks_url)
    return _jwks_client


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a Supabase access token; raises jwt exceptions on failure."""
    settings = get_settings()
    options = {"verify_aud": False}  # Supabase aud is "authenticated"
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            options=options,
        )
    except Exception:
        # Fall back to the legacy shared HS256 secret if available.
        if settings.supabase_jwt_secret:
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                options=options,
            )
        raise
