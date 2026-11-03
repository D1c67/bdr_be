"""Phase 8 triage settings and their boot validator (plan 13).

Pinned: the defaults, the dependency on the proactive router, the model price
rule (an OpenAI row in llm_pricing.PRICES), the lease rule (the RFP lease
pattern), the output allowance, the ranges, and the live-test spend cap
(0 to 5 USD, dev project only, allowlist required). No database, no network.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import _PHASE7_DEV_SUPABASE_REF, Settings

DEV_URL = f"https://{_PHASE7_DEV_SUPABASE_REF}.supabase.co"
PROD_LIKE_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
LOCAL_URL = "http://localhost:11434/v1"


def make(**kw) -> Settings:
    base = dict(
        assistant_enabled=True,
        assistant_token_secret="x" * 40,
        assistant_proactive_enabled=True,
        supabase_url=DEV_URL,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def refused(match: str, **kw) -> None:
    with pytest.raises(ValidationError, match=match):
        make(**kw)


def test_defaults_match_the_plan():
    s = Settings(_env_file=None)
    assert s.assistant_triage_enabled is False
    assert s.assistant_triage_loop_enabled is True
    assert s.assistant_triage_user_allowlist == ""
    assert s.assistant_triage_model == "gpt-6-luna"
    assert (s.assistant_triage_poll_seconds, s.assistant_triage_debounce_seconds) == (60, 300)
    assert (s.assistant_triage_batch_max, s.assistant_triage_batches_per_tick) == (10, 5)
    assert (s.assistant_triage_lease_seconds, s.assistant_triage_timeout_seconds) == (420, 45)
    assert s.assistant_triage_max_output_tokens == 3200
    assert s.assistant_triage_reasoning_effort == ""
    assert s.assistant_triage_window_hours == 24
    assert (s.assistant_triage_fuse_ratio, s.assistant_triage_fuse_per_hour) == (0.5, 30)
    assert s.assistant_triage_cap_pause_seconds == 900
    assert s.assistant_triage_retention_days == 90
    assert s.assistant_triage_test_spend_cap_usd is None


def test_the_defaults_boot_with_the_assistant_on():
    assert make().assistant_triage_model == "gpt-6-luna"


def test_no_floor_delegation_prep_or_bell_text_settings_exist():
    names = set(Settings.model_fields)
    for gone in (
        "assistant_triage_floor_usd",
        "assistant_triage_delegation",
        "assistant_triage_prep_enabled",
        "assistant_triage_due_soon_hours",
        "assistant_triage_include_bell_text",
    ):
        assert gone not in names


def test_triage_needs_the_proactive_router():
    refused(
        "ASSISTANT_TRIAGE_ENABLED=true needs ASSISTANT_PROACTIVE_ENABLED=true",
        assistant_triage_enabled=True,
        assistant_proactive_enabled=False,
        assistant_enabled=False,
    )
    assert make(assistant_triage_enabled=True).assistant_triage_enabled is True


@pytest.mark.parametrize("model", ["gpt-9-unpriced", "claude-opus-5-5", "gpt-6-luna-mini"])
def test_model_needs_an_openai_price_row(model):
    refused("ASSISTANT_TRIAGE_MODEL", assistant_triage_model=model)


def test_a_priced_openai_model_and_a_snapshot_pass():
    assert make(assistant_triage_model="gpt-5.4-mini").assistant_triage_model == "gpt-5.4-mini"
    assert make(assistant_triage_model="gpt-6-luna-2026-10-01")


def test_lease_must_outlive_one_worst_case_call():
    # 180 background wait + 45 x (1 + 1) + 60 = 330 with today's defaults.
    assert make(assistant_triage_lease_seconds=330).assistant_triage_lease_seconds == 330
    refused("ASSISTANT_TRIAGE_LEASE_SECONDS must be at least", assistant_triage_lease_seconds=329)
    # A longer timeout or more SDK retries raises the floor.
    refused(
        "ASSISTANT_TRIAGE_LEASE_SECONDS must be at least",
        assistant_triage_timeout_seconds=120,
        assistant_triage_lease_seconds=420,
    )
    refused(
        "ASSISTANT_TRIAGE_LEASE_SECONDS must be at least",
        third_party_llm_max_retries=4,
        assistant_triage_lease_seconds=420,
    )
    assert make(
        third_party_llm_max_retries=4, assistant_triage_lease_seconds=480
    ).assistant_triage_lease_seconds == 480


def test_an_unset_lease_is_stretched_not_refused():
    # 180 + 45 x 5 + 60 = 465 at four SDK retries; the default 420 is too short.
    s = make(third_party_llm_max_retries=4)
    assert s.assistant_triage_lease_seconds == 465
    assert make(third_party_llm_max_retries=4, assistant_triage_enabled=True)
    # A long background wait stretches it too (ceil of a fractional need).
    wide = dict(llm_queue_lease_seconds=3000)  # keeps the unrelated queue lease rule quiet
    assert make(llm_background_wait_seconds=270, **wide).assistant_triage_lease_seconds == 420
    assert make(llm_background_wait_seconds=300, **wide).assistant_triage_lease_seconds == 450
    assert make(llm_background_wait_seconds=300.5, **wide).assistant_triage_lease_seconds == 451
    # Defaults stay untouched when they already suffice.
    assert make().assistant_triage_lease_seconds == 420


def test_a_stretch_past_the_range_only_refuses_when_triage_runs():
    huge = dict(llm_background_wait_seconds=1700, llm_queue_lease_seconds=3000)
    assert make(**huge).assistant_triage_lease_seconds == 420  # triage off: left alone
    refused("ASSISTANT_TRIAGE_LEASE_SECONDS must be at least", assistant_triage_enabled=True, **huge)


def test_triage_under_the_self_hosted_master_switch_needs_the_openai_allow_flag():
    refused(
        "ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED",
        assistant_triage_enabled=True,
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url=LOCAL_URL,
    )
    s = make(
        assistant_triage_enabled=True,
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url=LOCAL_URL,
        assistant_allow_openai_in_self_hosted=True,
    )
    assert s.assistant_triage_enabled is True


def test_max_output_tokens_must_fit_a_full_batch():
    # P8L-3: 1200 + 200 x ASSISTANT_TRIAGE_BATCH_MAX.
    refused("ASSISTANT_TRIAGE_MAX_OUTPUT_TOKENS must be at least", assistant_triage_max_output_tokens=3199)
    assert make(assistant_triage_max_output_tokens=3200)
    refused("ASSISTANT_TRIAGE_MAX_OUTPUT_TOKENS must be at least", assistant_triage_batch_max=20)
    assert make(assistant_triage_batch_max=20, assistant_triage_max_output_tokens=5200)
    assert make(assistant_triage_batch_max=1, assistant_triage_max_output_tokens=1400)


def test_the_validator_formula_matches_the_model_module():
    from app.services import assistant_triage_model as model

    for batch in (1, 10, 20):
        floor = model.OUTPUT_TOKENS_BASE + model.OUTPUT_TOKENS_PER_ITEM * batch
        assert make(assistant_triage_batch_max=batch, assistant_triage_max_output_tokens=max(floor, 1400))
        if floor > 1400:
            refused("ASSISTANT_TRIAGE_MAX_OUTPUT_TOKENS must be at least",
                    assistant_triage_batch_max=batch, assistant_triage_max_output_tokens=floor - 1)
    assert Settings(_env_file=None).assistant_triage_max_output_tokens == model.DEFAULT_MAX_OUTPUT_TOKENS


@pytest.mark.parametrize(("raw", "stored"), [("low", "low"), (" MEDIUM ", "medium"), ("high", "high"), ("", "")])
def test_reasoning_effort_values(raw, stored):
    assert make(assistant_triage_reasoning_effort=raw).assistant_triage_reasoning_effort == stored


@pytest.mark.parametrize("raw", ["minimal", "max", "none"])
def test_reasoning_effort_refuses_other_values(raw):
    refused("ASSISTANT_TRIAGE_REASONING_EFFORT must be", assistant_triage_reasoning_effort=raw)


@pytest.mark.parametrize(
    "name,low,high",
    [
        ("assistant_triage_poll_seconds", 15, 600),
        ("assistant_triage_debounce_seconds", 0, 3600),
        ("assistant_triage_batch_max", 1, 20),
        ("assistant_triage_batches_per_tick", 1, 20),
        ("assistant_triage_timeout_seconds", 10, 120),
        ("assistant_triage_window_hours", 1, 72),
        ("assistant_triage_fuse_ratio", 0, 1),
        ("assistant_triage_fuse_per_hour", 0, 5000),
        ("assistant_triage_cap_pause_seconds", 60, 3600),
        ("assistant_triage_retention_days", 7, 3650),
    ],
)
def test_ranges(name, low, high):
    extra = {}
    if name == "assistant_triage_timeout_seconds":
        extra = {"assistant_triage_lease_seconds": 1800}
    if name == "assistant_triage_batch_max":
        extra = {"assistant_triage_max_output_tokens": 6000}
    for ok in (low, high):
        assert getattr(make(**{name: ok, **extra}), name) == ok
    for bad in (low - 1, high + 1):
        refused(name.upper(), **{name: bad, **extra})


def test_lease_and_output_ranges():
    refused("ASSISTANT_TRIAGE_LEASE_SECONDS must be between", assistant_triage_lease_seconds=59)
    refused("ASSISTANT_TRIAGE_LEASE_SECONDS must be between", assistant_triage_lease_seconds=1801)
    refused("ASSISTANT_TRIAGE_MAX_OUTPUT_TOKENS must be between", assistant_triage_max_output_tokens=1399)
    refused("ASSISTANT_TRIAGE_MAX_OUTPUT_TOKENS must be between", assistant_triage_max_output_tokens=6001)
    assert make(assistant_triage_max_output_tokens=6000, assistant_triage_batch_max=20)


def test_knobs_are_not_enforced_while_the_assistant_is_off():
    s = Settings(_env_file=None, assistant_triage_poll_seconds=1)
    assert s.assistant_triage_poll_seconds == 1


class TestTestSpendCap:
    ALLOW = "someone@example.com"

    def test_unset_is_normal_everywhere(self):
        assert make(supabase_url=PROD_LIKE_URL).assistant_triage_test_spend_cap_usd is None

    @pytest.mark.parametrize("cap", [0, 0.5, 1, 5])
    def test_in_range_on_dev_with_an_allowlist(self, cap):
        s = make(assistant_triage_test_spend_cap_usd=cap, assistant_triage_user_allowlist=self.ALLOW)
        assert s.assistant_triage_test_spend_cap_usd == cap

    @pytest.mark.parametrize("cap", [-0.01, 5.01, 100])
    def test_outside_zero_to_five_is_refused(self, cap):
        refused(
            "TEST_SPEND_CAP_USD must be between 0 and 5",
            assistant_triage_test_spend_cap_usd=cap,
            assistant_triage_user_allowlist=self.ALLOW,
        )

    def test_refused_without_an_allowlist(self):
        refused("needs ASSISTANT_TRIAGE_USER_ALLOWLIST", assistant_triage_test_spend_cap_usd=1)
        refused(
            "needs ASSISTANT_TRIAGE_USER_ALLOWLIST",
            assistant_triage_test_spend_cap_usd=1,
            assistant_triage_user_allowlist=" , ",
        )

    @pytest.mark.parametrize("url", [PROD_LIKE_URL, "https://qpfuugcwsdfpxvdgwvwx.supabase.co", ""])
    def test_refused_off_the_dev_project(self, url):
        refused(
            "allowed only on the dev Supabase project",
            supabase_url=url,
            assistant_triage_test_spend_cap_usd=1,
            assistant_triage_user_allowlist=self.ALLOW,
        )

    def test_the_cap_checks_run_even_with_the_assistant_off(self):
        with pytest.raises(ValidationError, match="TEST_SPEND_CAP_USD"):
            Settings(_env_file=None, assistant_triage_test_spend_cap_usd=1, supabase_url=PROD_LIKE_URL)
