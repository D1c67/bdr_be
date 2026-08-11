"""Singleton Supabase client using the service-role key (backend only).

The service role bypasses RLS, so every router MUST enforce authorization
explicitly via the deps in `app.core.deps`. RLS remains enabled in the DB as a
defense-in-depth backstop for any path that uses an end-user token.
"""

from functools import lru_cache

import httpx
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

from app.core.config import get_settings


@lru_cache
def get_supabase() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    # One HTTP/1.1 client shared by the PostgREST, storage, auth and functions
    # sub-clients. The library default gives each sub-client its own HTTP/2
    # session, so every threadpool request multiplexes over a single TCP
    # connection; one server GOAWAY (seen in prod while a large upload streamed
    # to storage) then kills every in-flight request on that connection, and the
    # collateral requests die with RemoteProtocolError "ConnectionTerminated" in
    # handlers that never touched storage. With HTTP/1.1 each request checks out
    # its own pooled connection, so a dropped connection fails only itself.
    # The single Timeout replaces the per-sub-client defaults; storage's default
    # was 20s, too tight for a 300 MB body (write= is per socket send, not the
    # whole upload, so large bodies need no special casing here).
    http_client = httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=30.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options=SyncClientOptions(httpx_client=http_client),
    )
