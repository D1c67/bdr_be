"""Provider-neutral parts, turns and request types (services/llm_parts).

Pinned here:
  parts       legacy Responses dict parts round-trip byte for byte through
              from_responses_part / to_responses_parts; the Anthropic
              encoder emits image and document base64 blocks and puts a
              cache_control marker only on cache_break text parts; empty
              text blocks are dropped for Anthropic.
  request     tool_choice is auto only; effort, max_tokens and the
              breakpoint count are validated; the first turn is a UserTurn;
              tools are offered sorted by name and not at all when disabled.
  images      OpenAI tokens follow the 32 px patch math capped at 30,000
              patches; Anthropic tokens are pixels / 750 capped at 4,784;
              fits/scale_to_fit respect the long edge (2,576) and 3.75 MP
              limits; check_limits refuses more than 90 images or 30 MB on
              the Anthropic route and flags oversized images.
  turn        ModelTurn.assistant_turn carries tool calls and raw blocks.

(tests/test_llm_parts.py is the older OpenAI complete_parts suite; this
file covers the neutral module added by the AI Estimating build.)
"""

from __future__ import annotations

import base64
import math
from decimal import Decimal

import pytest

from app.services import llm_parts as lp
from app.services.llm import LlmUsage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.4 fake"


def _legacy_parts() -> list[dict]:
    return [
        {"type": "input_text", "text": "Ledger"},
        {"type": "input_image", "image_url": lp.data_url("image/png", PNG), "detail": "original"},
        {"type": "input_image", "image_url": lp.data_url("image/jpeg", PNG), "detail": "high"},
        {"type": "input_file", "filename": "spec.pdf", "file_data": lp.data_url("application/pdf", PDF)},
    ]


def test_legacy_parts_round_trip_byte_identical():
    legacy = _legacy_parts()
    neutral = [lp.from_responses_part(p) for p in legacy]
    assert isinstance(neutral[0], lp.TextPart)
    assert isinstance(neutral[1], lp.ImagePart) and neutral[1].png == PNG and neutral[1].detail == "original"
    assert neutral[2].media_type == "image/jpeg" and neutral[2].detail == "high"
    assert isinstance(neutral[3], lp.FilePart) and neutral[3].data == PDF and neutral[3].filename == "spec.pdf"
    assert lp.to_responses_parts(neutral) == legacy


def test_from_responses_part_rejects_non_data_urls_and_unknown_types():
    with pytest.raises(ValueError):
        lp.from_responses_part({"type": "input_image", "image_url": "https://example.com/a.png"})
    with pytest.raises(ValueError):
        lp.from_responses_part({"type": "input_image", "image_url": "data:image/png;base64,###"})
    with pytest.raises(ValueError):
        lp.from_responses_part({"type": "output_text", "text": "x"})


def test_anthropic_blocks_shape_and_cache_break():
    parts = [
        lp.TextPart("Ledger", cache_break=True),
        lp.TextPart(""),
        lp.ImagePart(PNG, 10, 10),
        lp.FilePart("spec.pdf", PDF),
        lp.TextPart("Question"),
    ]
    blocks = lp.to_anthropic_blocks(parts)
    assert [b["type"] for b in blocks] == ["text", "image", "document", "text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[3]
    assert blocks[1]["source"] == {
        "type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode(),
    }
    assert blocks[2]["source"]["media_type"] == "application/pdf"


def test_image_part_validates_detail():
    with pytest.raises(ValueError):
        lp.ImagePart(PNG, detail="ultra")


def _req(**over) -> lp.ModelRequest:
    base = dict(
        feature="plans_recount",
        system="sys",
        messages=(lp.UserTurn((lp.TextPart("hello"),)),),
    )
    base.update(over)
    return lp.ModelRequest(**base)


def test_request_validation():
    with pytest.raises(ValueError, match="auto"):
        _req(tool_choice="any")
    with pytest.raises(ValueError, match="effort"):
        _req(effort="ultra")
    with pytest.raises(ValueError, match="max_tokens"):
        _req(max_tokens=0)
    with pytest.raises(ValueError, match="cache_breakpoints"):
        _req(cache_breakpoints=5)
    with pytest.raises(ValueError, match="at least one turn"):
        _req(messages=())
    with pytest.raises(ValueError, match="UserTurn"):
        _req(messages=(lp.AssistantTurn("openai", "m", "r"),))
    with pytest.raises(ValueError, match="unique"):
        _req(tools=(lp.ToolDef("a", "d", {}), lp.ToolDef("a", "d", {})))


def test_tools_offered_sorted_and_hidden_when_disabled():
    tools = (lp.ToolDef("zoom_region", "z", {}), lp.ToolDef("census", "c", {}))
    assert [t.name for t in _req(tools=tools).tools_offered] == ["census", "zoom_region"]
    assert _req(tools=tools, tools_enabled=False).tools_offered == ()


def test_openai_image_tokens_match_patch_math_and_cap():
    spec = lp.OPENAI_IMAGES
    assert spec.image_tokens(64, 64) == math.ceil(4 * 1.2)
    big = spec.image_tokens(20_000, 20_000)
    assert big == math.ceil(30_000 * 1.2)
    assert spec.fits(2048, 2048) is True
    assert spec.fits(20_000, 20_000) is False
    w, h = spec.scale_to_fit(20_000, 20_000)
    assert spec.fits(w, h) and w == h


def test_anthropic_image_tokens_and_limits():
    spec = lp.ANTHROPIC_IMAGES
    assert spec.image_tokens(750, 100) == 100
    assert spec.image_tokens(2576, 1456) == min(4784, math.ceil(2576 * 1456 / 750))
    assert spec.image_tokens(5000, 5000) == 4784
    assert spec.fits(2576, 1400) is True
    assert spec.fits(2577, 100) is False  # long edge
    assert spec.fits(2000, 2000) is False  # 4 MP > 3.75 MP
    w, h = spec.scale_to_fit(6510, 4650)
    assert spec.fits(w, h)
    assert abs(w / h - 6510 / 4650) < 0.01
    assert spec.max_images_per_request == 90
    assert spec.max_request_bytes == 30 * 1024 * 1024
    with pytest.raises(ValueError):
        lp.image_spec_for("self_hosted")


def test_check_limits_counts_images_bytes_and_oversized():
    turns = (
        lp.UserTurn((lp.TextPart("a"), lp.ImagePart(PNG, 100, 100))),
        lp.AssistantTurn("anthropic", "m", "r1", tool_calls=(lp.ToolCall("c1", "zoom_region", {}),)),
        lp.ToolResultsTurn(
            (lp.ToolResultMsg("c1", "ok", images=(lp.ImagePart(PNG, 3000, 3000),)),),
            extra_parts=(lp.TextPart("Tool use is finished."),),
        ),
    )
    limits = lp.check_limits(turns, "anthropic")
    assert limits.images == 2
    assert limits.oversized_images == (3,)  # parts order: a, img, "ok", img
    assert limits.ok is False
    assert "pixel" in limits.problem()
    ok = lp.check_limits(turns, "openai")
    assert ok.ok is True and ok.problem() is None
    too_many = (lp.UserTurn(tuple(lp.ImagePart(PNG, 10, 10) for _ in range(91))),)
    assert "91 images" in lp.check_limits(too_many, "anthropic").problem()
    assert lp.check_limits(too_many, "openai").ok is True


def test_payload_bytes_is_base64_expanded():
    parts = [lp.TextPart("abc"), lp.ImagePart(b"x" * 300, 1, 1), lp.FilePart("f.pdf", b"y" * 30)]
    assert lp.payload_bytes(parts) == 3 + 400 + 40
    with pytest.raises(ValueError):
        lp.payload_bytes(parts, "self_hosted")


def test_estimate_part_tokens_adds_text_images_and_files():
    parts = [lp.TextPart("x" * 300), lp.ImagePart(PNG, 750, 100), lp.FilePart("f.pdf", b"y" * 30)]
    assert lp.estimate_part_tokens(parts, "anthropic") == 100 + 100 + 10
    assert lp.estimate_part_tokens([lp.ImagePart(PNG)], "anthropic") == 4784
    assert lp.estimate_part_tokens([lp.ImagePart(PNG)], "openai") == math.ceil(30_000 * 1.2)


def test_model_turn_to_assistant_turn_and_tool_call_dicts():
    turn = lp.ModelTurn(
        text="t", parsed=None, tool_calls=[{"call_id": "c1", "name": "census", "args": {"page": 3}}],
        usage=LlmUsage(10, 0, 5, 0), cost_usd=Decimal("0.001"), response_id="r1",
        stop_reason="tool_use", provider="anthropic", model="claude-opus-5-5",
        raw_assistant=({"type": "thinking", "thinking": "", "signature": "s"},),
    )
    at = turn.assistant_turn()
    assert at.tool_calls == (lp.ToolCall("c1", "census", {"page": 3}),)
    assert at.raw[0]["type"] == "thinking"
    assert lp.tool_call_dicts([{"call_id": "c2", "name": "n", "arguments": {"a": 1}}]) == [
        {"call_id": "c2", "name": "n", "args": {"a": 1}}
    ]
    assert lp.tool_call_dicts(at.tool_calls) == [{"call_id": "c1", "name": "census", "args": {"page": 3}}]
