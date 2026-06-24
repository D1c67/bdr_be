"""Singleton Supabase client using the service-role key (backend only).

The service role bypasses RLS, so every router MUST enforce authorization
explicitly via the deps in `app.core.deps`. RLS remains enabled in the DB as a
defense-in-depth backstop for any path that uses an end-user token.
"""

from functools import lru_cache

from supabase import Client, create_client

from app.core.config import get_settings


@lru_cache
def get_supabase() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
