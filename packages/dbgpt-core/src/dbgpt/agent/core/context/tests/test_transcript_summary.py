"""Tests for the summary producer (Phase 2, §3.3/§3.4)."""

import pytest

from dbgpt.agent.core.context.budget import ContextBudgetConfig
from dbgpt.agent.core.context.transcript_compaction import (
    assemble_summary_draft,
    decide_summary,
    select_summary_region,
)
from dbgpt.agent.core.context.transcript_summary import (
    HANDOFF_TEMPLATE,
    HYBRID_TEMPLATE,
    SECTIONS6_TEMPLATE,
    SUMMARY_END_MARKER,
    build_summary_message_content,
    build_summary_prompt,
    is_context_overflow_error,
    serialize_region,
    summarize_region,
    summary_template_for,
)
from dbgpt.core.interface.message import ModelMessage
from dbgpt.util.error_types import LLMChatError


def _human(content="q"):
    return ModelMessage(role="human", content=content)


def _ai(content="a", tool_calls=None):
    return ModelMessage(role="ai", content=content, tool_calls=tool_calls)


def _tool(call_id, content="r"):
    return ModelMessage(role="tool", content=content, tool_call_id=call_id)


def _call(call_id, name="query", arguments='{"sql":"select 1"}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _config(**overrides):
    values = dict(
        context_compaction_enabled=True,
        proactive_prune_tokens=1000,
        min_reclaim_tokens=100,
        protect_tail_tokens=0,
        protect_last_rounds=1,
        max_tail_rounds_floor=8,
        min_prune_chars=1000,
    )
    values.update(overrides)
    return ContextBudgetConfig(**values)


# ---------------------------------------------------------------------------
# Input assembly (§3.3)
# ---------------------------------------------------------------------------


def test_serialize_region_keeps_full_content_and_tool_calls():
    big = "Z" * 20000
    messages = [
        _human("first question"),
        _ai("", [_call("c1", name="sql_query")]),
        _tool("c1", big),
    ]

    text = serialize_region(messages)

    assert "[human]: first question" in text
    assert "[tool_calls] sql_query({\"sql\":\"select 1\"})" in text
    # Full content: no [:3000] style truncation anywhere.
    assert f"[tool]: {big}" in text


def test_summary_prompt_uses_the_sections6_template():
    messages = [_human("only message")]

    prompt = build_summary_prompt(messages)

    assert "## Conversation to summarize" in prompt
    assert "1. Original Task" in prompt
    assert "6. Next Steps" in prompt
    assert "[human]: only message" in prompt
    assert prompt.count("## Conversation to summarize") == 1
    assert SECTIONS6_TEMPLATE.count("{conversation_text}") == 1


def test_summary_message_content_matches_the_legacy_prefix():
    content = build_summary_message_content("condensed", 7)

    assert content.startswith("[Context Summary of 7 earlier messages]\n\n")
    assert "condensed" in content
    assert content.endswith(SUMMARY_END_MARKER)


def test_overflow_classification_matches_text_patterns():
    assert is_context_overflow_error(ValueError("maximum context length exceeded"))
    assert is_context_overflow_error(ValueError("Prompt is too long"))
    assert is_context_overflow_error(ValueError("error 413 from provider"))
    assert is_context_overflow_error(
        LLMChatError(original_exception=ValueError("please reduce the length"))
    )
    assert not is_context_overflow_error(RuntimeError("provider exploded"))


# ---------------------------------------------------------------------------
# Region selection (§3.3/F2)
# ---------------------------------------------------------------------------


def test_summary_region_takes_the_contiguous_compressible_prefix():
    big = "Z" * 3000
    messages = [
        _human("t1"),
        _ai("", [_call("a1")]),
        _tool("a1", big),
        _human("t2"),
        _ai("", [_call("a2")]),
        _tool("a2", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=1)

    region = select_summary_region(messages, config)

    assert region.start == 0
    assert region.end == 6  # t1 + t2, current turn excluded
    assert region.turn_count == 2
    assert region.region_tokens > 0


def test_summary_region_stops_at_a_turn_with_unresolved_calls():
    big = "Z" * 3000
    messages = [
        _human("t1"),
        _ai("", [_call("a1")]),
        _tool("a1", big),
        _human("t2"),
        _ai("", [_call("done"), _call("unknown")]),
        _tool("done", big),
        _human("t3"),
        _ai("", [_call("c2")]),
        _tool("c2", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=1)

    region = select_summary_region(
        messages, config, protect_ids=frozenset({"unknown"})
    )

    # t1 absorbed; t2 (containing the unresolved call) blocks the region.
    assert region.end == 3
    assert region.turn_count == 1


def test_summary_region_absorbs_an_old_summary_segment():
    summary = _human("[Context Summary of 4 earlier messages]\n\nold summary")
    big = "Z" * 3000
    messages = [
        summary,
        _ai("follow-up"),
        _human("t2"),
        _ai("", [_call("a2")]),
        _tool("a2", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=1)

    region = select_summary_region(messages, config)

    # The summary-led segment does not block: it is absorbed with the block.
    assert region.end == 5
    assert region.turn_count == 2


def test_summary_region_stops_at_a_real_orphan_and_at_the_tail():
    big = "Z" * 3000
    orphaned = [
        _ai("", [_call("x")]),
        _tool("x", big),
        _human("t1"),
        _ai("done"),
        _human("current"),
        _ai("done"),
    ]

    region = select_summary_region(orphaned, _config(protect_last_rounds=1))

    # Non-summary orphan head blocks everything.
    assert region.empty

    tail_only = [
        _human("t1"),
        _ai("", [_call("a1")]),
        _tool("a1", big),
    ]
    region = select_summary_region(tail_only, _config(protect_last_rounds=1))

    # The single turn is the current turn / tail: nothing to absorb.
    assert region.empty


# ---------------------------------------------------------------------------
# Trigger chain (§3.2) and draft assembly (§3.8)
# ---------------------------------------------------------------------------


class _FakeProjection:
    def __init__(
        self, messages, synthetic_tool_call_ids=frozenset(), latest_event_seq=9
    ):
        self.messages = tuple(messages)
        self.synthetic_tool_call_ids = frozenset(synthetic_tool_call_ids)
        self.latest_event_seq = latest_event_seq
        self.last_compaction = None


def _summary_projection():
    big = "Z" * 20000
    return _FakeProjection(
        [
            _human("t1"),
            _ai("", [_call("a1")]),
            _tool("a1", big),
            _human("current"),
            _ai("done"),
        ]
    )


def test_decide_summary_gates_in_order():
    projection = _summary_projection()
    threshold_config = _config(compact_threshold_tokens=1000)

    passed = decide_summary(projection, threshold_config, 200000)
    assert passed.gate == "passed"
    assert passed.should_summarize
    assert passed.region.turn_count == 1
    # assumed_reclaim ~= region * 0.8
    assert passed.assumed_reclaim == passed.region_tokens - int(
        passed.region_tokens * 0.2
    )

    below = decide_summary(
        projection, _config(compact_threshold_tokens=10**9), 200000
    )
    assert below.gate == "threshold"

    watermark_bound = decide_summary(
        projection,
        _config(compact_threshold_tokens=1000, proactive_prune_tokens=10**9),
        200000,
    )
    assert watermark_bound.gate == "watermark"

    projection.last_compaction = type(
        "S", (), {"rearm_tokens": 10**9, "through_event_seq": 1, "accounting": {}}
    )()
    rearm_bound = decide_summary(
        projection, _config(compact_threshold_tokens=1000), 200000
    )
    assert rearm_bound.gate == "rearm"
    projection.last_compaction = None

    tail_only = _FakeProjection([_human("only"), _ai("x" * 20000)])
    empty = decide_summary(
        tail_only,
        _config(compact_threshold_tokens=1000, protect_last_rounds=3),
        200000,
    )
    assert empty.gate == "region_empty"

    reclaim = decide_summary(
        projection,
        _config(compact_threshold_tokens=1000, min_reclaim_tokens=10**9),
        200000,
    )
    assert reclaim.gate == "reclaim"
    assert reclaim.region_tokens > 0


def test_assemble_summary_draft_splices_the_region_and_accounts():
    projection = _summary_projection()
    region = select_summary_region(projection.messages, _config())
    content = build_summary_message_content("condensed text", region.turn_count)

    draft = assemble_summary_draft(projection, region, content, _config(), 200000)

    assert draft is not None
    assert draft.through_event_seq == 9
    assert draft.replacement_history[0] == {"role": "human", "content": content}
    assert draft.replacement_history[-1]["content"] == "done"
    accounting = draft.accounting
    assert accounting["summary_chars"] == len(content)
    assert accounting["reclaimed"] == accounting["tokens_before"] - accounting[
        "tokens_after"
    ]
    assert accounting["reclaimed"] > 0


def test_assemble_summary_draft_returns_none_without_reduction():
    projection = _summary_projection()
    region = select_summary_region(projection.messages, _config())
    huge = "X" * (region.region_tokens * 4 + 1000)
    content = build_summary_message_content(huge, region.turn_count)

    draft = assemble_summary_draft(projection, region, content, _config(), 200000)

    assert draft is None


def test_assemble_summary_draft_strips_synthetic_entries_outside_the_region():
    big = "Z" * 20000
    projection = _FakeProjection(
        [
            _human("t1"),
            _ai("", [_call("a1")]),
            _tool("a1", big),
            _human("t2"),
            _ai("", [_call("done"), _call("unknown")]),
            _tool("done", "D" * 3000),
            _tool("unknown", "aborted"),
            _human("current"),
            _ai("done"),
        ],
        synthetic_tool_call_ids={"unknown"},
    )
    config = _config()
    region = select_summary_region(projection.messages, config)
    content = build_summary_message_content("condensed", region.turn_count)

    draft = assemble_summary_draft(projection, region, content, config, 200000)

    assert draft is not None
    assert all(
        entry.get("content") != "aborted" for entry in draft.replacement_history
    )


# ---------------------------------------------------------------------------
# Trim-retry (§3.3/F4)
# ---------------------------------------------------------------------------


class _StubSummaryClient:
    """Minimal AIWrapper-shaped stub capturing create() calls."""

    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        action = self.script.pop(0) if self.script else "OK summary"
        if isinstance(action, Exception):
            raise action
        return action


def _trim_scenario_turns():
    def turn(index):
        return [
            _human(f"turn {index} content-marker-{index}"),
            _ai("", [_call(f"c{index}")]),
            _tool(f"c{index}", f"result-{index}-" + "R" * 500),
        ]

    return [*turn(1), *turn(2), *turn(3)]


@pytest.mark.asyncio
async def test_trim_retry_trims_the_oldest_turn_and_succeeds():
    messages = _trim_scenario_turns()
    overflow = ValueError("maximum context length exceeded")
    client = _StubSummaryClient(script=[overflow, "trimmed summary"])

    text, llm_calls = await summarize_region(
        messages, client, "stub-model", _config()
    )

    assert text == "trimmed summary"
    assert llm_calls == 2
    assert len(client.calls) == 2
    # F7: the summary call is explicitly uncapped.
    assert client.calls[0]["max_new_tokens"] is None
    first_prompt = client.calls[0]["messages"][0]["content"]
    second_prompt = client.calls[1]["messages"][0]["content"]
    assert "content-marker-1" in first_prompt
    assert "content-marker-1" not in second_prompt
    assert "content-marker-2" in second_prompt


@pytest.mark.asyncio
async def test_trim_retry_gives_up_bounded_when_only_one_turn_remains():
    messages = _trim_scenario_turns()
    overflow = ValueError("prompt is too long")
    client = _StubSummaryClient(script=[overflow, overflow, overflow])

    with pytest.raises(ValueError, match="prompt is too long"):
        await summarize_region(messages, client, "stub-model", _config())

    # Three turns -> three attempts (last one on the single remaining turn).
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_non_overflow_errors_do_not_trim():
    messages = _trim_scenario_turns()
    client = _StubSummaryClient(script=[RuntimeError("provider exploded")])

    with pytest.raises(RuntimeError, match="provider exploded"):
        await summarize_region(messages, client, "stub-model", _config())

    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Template switch (§3.7)
# ---------------------------------------------------------------------------


def test_summary_template_switch_selects_all_three_variants():
    messages = [_human("only message")]

    sections6 = build_summary_prompt(
        messages, summary_template_for(_config(summary_template="sections6"))
    )
    assert "1. Original Task" in sections6
    assert "6. Next Steps" in sections6
    assert "User Constraints & Preferences" not in sections6

    handoff = build_summary_prompt(
        messages, summary_template_for(_config(summary_template="handoff"))
    )
    assert "3. What Remains" in handoff
    assert "Original Task" not in handoff

    hybrid = build_summary_prompt(
        messages, summary_template_for(_config(summary_template="hybrid"))
    )
    assert "User Constraints & Preferences" in hybrid
    assert "1. Original Task" in hybrid
    assert "another LLM that will resume this task" in hybrid
    assert "must not be redone" in hybrid

    assert SECTIONS6_TEMPLATE != HANDOFF_TEMPLATE != HYBRID_TEMPLATE


def test_summary_template_defaults_to_hybrid_and_falls_back_on_unknown():
    assert _config().summary_template == "hybrid"
    assert summary_template_for(_config()) == HYBRID_TEMPLATE
    assert summary_template_for(_config(summary_template="nonsense")) == (
        HYBRID_TEMPLATE
    )


@pytest.mark.asyncio
async def test_summarize_region_uses_the_configured_template():
    messages = _trim_scenario_turns()
    client = _StubSummaryClient()

    await summarize_region(
        messages, client, "stub-model", _config(summary_template="handoff")
    )

    prompt = client.calls[0]["messages"][0]["content"]
    assert "3. What Remains" in prompt
    assert "User Constraints & Preferences" not in prompt
