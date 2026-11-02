from pathlib import Path

SQL = (Path(__file__).parents[1] / "supabase/migrations/0185_assistant_mail_dispatch_fences.sql").read_text()


def test_live_authority_and_attachment_fences():
    assert "'{request,json,authority,role}' is distinct from v_role" in SQL
    assert "v_conversation.mailbox_tainted_at is not null" in SQL
    assert "v_tainted and" in SQL
    assert "role::text in ('estimating_admin','executive','it_admin','accountant') for share" in SQL
    assert "size_bytes=(v_file->>'size_bytes')::bigint" in SQL
    assert "expires_at>now() and consumed_at is null and purged_at is null" in SQL
    assert "mime='application/pdf' for share" in SQL
    assert SQL.index("attachment_changed") < SQL.index(">=20")


def test_keyset_pagination_and_no_public_rpc():
    assert SQL.count("(created_at,id)<(p_before,p_before_id)") == 2
    for kind in ("mailbox", "evidence"):
        assert f"revoke all on function public.assistant_{kind}_access_page" in SQL
        assert f"revoke execute on function public.assistant_{kind}_access_list" in SQL
