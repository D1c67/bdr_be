"""Static defense contracts; not a substitute for transactional SQL tests."""
from pathlib import Path
import re

SQL = (Path(__file__).parents[1] / "supabase/migrations/0184_assistant_mail_operations.sql").read_text()


def test_functions_private_and_invoker():
    names = re.findall(r"create or replace function public\.(\w+)\(", SQL)
    assert len(names) >= 10
    assert SQL.count("security invoker set search_path=public,pg_temp") == len(names)
    for name in names:
        assert f"revoke all on function public.{name}(" in SQL
        assert f"grant execute on function public.{name}(" in SQL
    assert "security definer" not in SQL.lower()


def test_atomic_quota_and_replay_contract():
    assert SQL.count("pg_advisory_xact_lock") == 2
    assert "America/Los_Angeles" in SQL
    assert ">=20" in SQL and ">=10" in SQL and ">=3" in SQL
    assert "approval_id uuid not null unique" in SQL
    assert "a.payload_sha256 is distinct from p_payload_sha256" in SQL
    assert "a.payload#>>'{request,json,sender}' is distinct from p_sender" in SQL
    assert "select run_id into p_run_id" in SQL


def test_review_immutable_and_calendar_delivery_atomic():
    for table in ("assistant_mailbox_access", "assistant_mailbox_outcomes"):
        assert f"{table}_immutable" in SQL
        assert f"{table}_no_truncate" in SQL
        assert f"alter table public.{table} force row level security" in SQL
    calendar = SQL.split("function public.assistant_calendar_reminder_claim(", 1)[1].split("end $$;", 1)[0]
    assert "insert into public.assistant_inbox" in calendar
    assert "insert into public.notifications" in calendar
    assert "'assistant.calendar'" in calendar
