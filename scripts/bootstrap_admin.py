"""One-time bootstrap: create the first IT Admin user + profile.

The app is invite-only (IT Admin invites everyone else), so the very first admin
must be seeded directly. This uses the Supabase Auth admin API with the
service-role key, creating a confirmed user with a temporary password so you can
log in immediately (no invite email needed).

Usage:
    cd bdr_be
    uv run python scripts/bootstrap_admin.py "admin@g3electrical.com" "Full Name" "TempPass123!"
"""

import sys
from pathlib import Path

# Allow running as a plain script (`python scripts/bootstrap_admin.py`): put the
# project root (bdr_be) on the import path so `app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.supabase_client import get_supabase


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    email, full_name, password = sys.argv[1], sys.argv[2], sys.argv[3]
    sb = get_supabase()

    # Idempotent: create the auth user, or reuse + reset the password if the
    # email is already registered (e.g. a partial earlier run).
    user_id = _find_user_id(sb, email)
    if user_id is None:
        created = sb.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
        user_id = created.user.id
        action = "Created"
    else:
        sb.auth.admin.update_user_by_id(
            user_id, {"password": password, "email_confirm": True}
        )
        action = "Updated existing"

    sb.table("profiles").upsert(
        {
            "id": user_id,
            "email": email,
            "full_name": full_name,
            "role": "it_admin",
            "is_active": True,
        }
    ).execute()
    print(f"✅ {action} IT Admin {email} (id={user_id}). Log in with the temp password.")


def _find_user_id(sb, email: str) -> str | None:
    """Return the auth user id for `email`, paging through admin list_users."""
    page = 1
    while True:
        users = sb.auth.admin.list_users(page=page, per_page=1000)
        if not users:
            return None
        for u in users:
            if (u.email or "").lower() == email.lower():
                return u.id
        if len(users) < 1000:
            return None
        page += 1


if __name__ == "__main__":
    main()
