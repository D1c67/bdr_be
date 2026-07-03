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
    """Decode and verify a Supabase access token; raises jwt exceptions on failure.

    The verification path is selected by the token's declared algorithm, never by
    "try asymmetric, silently fall back to the shared secret on any error". That
    matters for two reasons: (1) a JWKS/network hiccup can no longer route an
    asymmetric token into the HS256 branch, and (2) an ES256/RS256 token can
    never be downgraded to shared-secret verification (algorithm confusion). The
    legacy HS256 branch runs only for tokens that actually declare alg=HS256, and
    only while it is explicitly enabled with a secret configured.

    Returns the full claim set; `app/core/deps.get_current_user` consumes the
    `aal` claim to enforce 2FA (this module verifies the signature, not assurance).
    """
    settings = get_settings()
    options = {"verify_aud": False}  # Supabase aud is "authenticated"
    alg = jwt.get_unverified_header(token).get("alg", "")

    if alg in ("RS256", "ES256"):
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key, algorithms=["RS256", "ES256"], options=options)

    if alg == "HS256" and settings.legacy_hs256_enabled and settings.supabase_jwt_secret:
        return jwt.decode(
            token, settings.supabase_jwt_secret, algorithms=["HS256"], options=options
        )

    raise jwt.InvalidAlgorithmError(
        f"Token algorithm '{alg or 'none'}' is not accepted"
    )
