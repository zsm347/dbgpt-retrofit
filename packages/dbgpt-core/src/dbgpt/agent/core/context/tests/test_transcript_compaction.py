"""Tests for structural transcript compaction (Phase 0 + 1/2, §3/§5/§2)."""

import json
import math
from dataclasses import dataclass
from typing import Any, Optional

from dbgpt.agent.core.context.budget import ContextBudgetConfig
from dbgpt.agent.core.context.transcript_compaction import (
    DEDUPE_REFERENCE_PREFIX,
    build_pruned_replacement,
    compact_threshold_tokens_for,
    context_status_state,
    decide_compaction,
    dedupe_replacement,
    detect_turns,
    estimate_tokens,
    select_window,
)
from dbgpt.core.interface.message import ModelMessage


def _human(content="q"):
    return ModelMessage(role="human", content=content)


def _ai(content="a", tool_calls=None):
    return ModelMessage(role="ai", content=content, tool_calls=tool_calls)


def _tool(call_id, content="r"):
    return ModelMessage(role="tool", content=content, tool_call_id=call_id)


def _system(content="sys"):
    return ModelMessage(role="system", content=content)


def _call(call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "query", "arguments": '{"sql":"select 1"}'},
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
# Turn detection (§3.2/§3.3)
# ---------------------------------------------------------------------------


def test_parallel_batch_and_tool_loops_stay_in_one_turn():
    messages = [
        _human("run both"),
        _ai("", [_call("a"), _call("b"), _call("c")]),
        _tool("a"),
        _tool("b"),
        _tool("c"),
        _ai("", [_call("d")]),
        _tool("d"),
        _ai("done"),
        _human("next"),
        _ai("answer"),
    ]

    turns = detect_turns(messages)

    assert [(turn.start, turn.indices) for turn in turns] == [
        (0, (0, 1, 2, 3, 4, 5, 6, 7)),
        (8, (8, 9)),
    ]
    assert all(not turn.is_orphan for turn in turns)


def test_plain_question_answer_is_a_two_message_turn():
    turns = detect_turns([_human("q"), _ai("a")])

    assert [(turn.start, turn.indices) for turn in turns] == [(0, (0, 1))]


def test_system_and_view_messages_do_not_participate_in_turns():
    messages = [_system("sys"), _human("q"), _ai("a")]

    turns = detect_turns(messages)

    assert [(turn.start, turn.indices) for turn in turns] == [(1, (1, 2))]


def test_history_starting_with_tool_messages_forms_a_protected_orphan_segment():
    messages = [
        _ai("", [_call("x")]),
        _tool("x"),
        _human("q"),
        _ai("a"),
        _human("q2"),
        _ai("a2"),
    ]

    turns = detect_turns(messages)

    assert turns[0].is_orphan is True
    assert turns[0].indices == (0, 1)
    window = select_window(messages, _config())
    assert window.compressible_indices == frozenset({2, 3})
    assert not window.empty


def test_empty_message_list_yields_no_turns_and_no_window():
    window = select_window([], _config())

    assert window.turns == ()
    assert window.empty


def test_new_session_with_only_system_and_human_has_nothing_to_compress():
    messages = [_system("sys"), _human("hi")]

    window = select_window(messages, _config())

    assert window.empty


def test_synthetic_call_turn_is_protected_while_other_turns_compress():
    messages = [
        _human("t1"),
        _ai("", [_call("done"), _call("unknown")]),
        _tool("done", "D" * 5000),
        _human("t2"),
        _ai("", [_call("c2")]),
        _tool("c2", "E" * 5000),
        _human("t3"),
        _ai("final"),
    ]

    window = select_window(
        messages, _config(), protect_ids=frozenset({"unknown"})
    )

    assert window.compressible_indices == frozenset({3, 4, 5})


def test_summary_marker_is_not_a_turn_start_and_protects_its_segment():
    summary = _human("[Context Summary of 4 earlier messages]\n\n…")
    messages = [
        _human("t1"),
        _ai("a1"),
        summary,
        _ai("a2"),
        _human("t2"),
        _ai("a3"),
        _human("t3"),
        _ai("a4"),
    ]

    turns = detect_turns(messages)

    # The summary message extends the first turn instead of starting one.
    assert [(turn.start, turn.indices) for turn in turns] == [
        (0, (0, 1, 2, 3)),
        (4, (4, 5)),
        (6, (6, 7)),
    ]
    window = select_window(messages, _config())
    # t1 is protected by the summary marker, t3 by the tail; t2 is prunable.
    assert window.compressible_indices == frozenset({4, 5})


def test_summary_at_the_head_forms_a_protected_orphan_segment():
    messages = [
        _human("[Context Summary of 2 earlier messages]\n\n…"),
        _ai("follow-up"),
        _human("real question"),
        _ai("answer"),
    ]

    window = select_window(messages, _config())

    assert window.turns[0].is_orphan is True
    assert window.empty


# ---------------------------------------------------------------------------
# Window selection (§3.4)
# ---------------------------------------------------------------------------


def test_tail_protection_keeps_at_least_the_configured_rounds():
    messages = []
    for index in range(5):
        messages.append(_human(f"q{index}"))
        messages.append(_ai("x" * 40))

    window = select_window(messages, _config(protect_last_rounds=3))

    # Last 3 turns (indices 4..9) protected, first 2 turns prunable.
    assert window.compressible_indices == frozenset({0, 1, 2, 3})


def test_tail_protection_extends_until_the_token_budget_is_reached():
    messages = []
    for index in range(5):
        messages.append(_human(f"q{index}"))
        messages.append(_ai("x" * 40))
    per_turn = estimate_tokens([messages[0], messages[1]])
    assert per_turn == 11  # ceil(("q0" + 40 chars) / 4)

    window = select_window(
        messages,
        _config(protect_last_rounds=1, protect_tail_tokens=25),
    )

    # 11 -> 22 -> 33 crosses the 25-token budget with the third tail turn.
    assert window.compressible_indices == frozenset({0, 1, 2, 3})


def test_tail_protection_is_capped_to_avoid_a_compaction_deadlock():
    messages = []
    for index in range(12):
        messages.append(_human(f"q{index}"))
        messages.append(_ai("x" * 4000))

    window = select_window(
        messages,
        _config(
            protect_last_rounds=3,
            protect_tail_tokens=10**9,
            max_tail_rounds_floor=8,
        ),
    )

    # Only the last 8 turns stay protected; the first 4 turns remain prunable.
    assert window.compressible_indices == frozenset(range(8))


# ---------------------------------------------------------------------------
# Replacement builder (§3.6)
# ---------------------------------------------------------------------------


def _prune_scenario():
    big = "B" * 5000
    persisted = "<persisted-output>\npreview…\n</persisted-output>"
    messages = [
        _human("t1"),
        _ai("", [_call("a")]),
        _tool("a", big),
        _human("t2"),
        _ai("", [_call("b")]),
        _tool("b", persisted),
        _human("t3"),
        _ai("", [_call("c")]),
        _tool("c", "small result"),
        _human("t4"),
        _ai("", [_call("d")]),
        _tool("d", "D" * 5000),
    ]
    return messages, big


def test_replacement_truncates_windowed_results_and_keeps_persisted_blocks():
    messages, big = _prune_scenario()
    config = _config()
    window = select_window(messages, config)

    replacement, stats = build_pruned_replacement(messages, window, config)

    by_call = {
        entry.get("tool_call_id"): entry["content"]
        for entry in replacement
        if entry["role"] == "tool"
    }
    assert by_call["a"] == (
        '[query] args≈{"sql":"select 1"} · success=? · 5000 chars'
    )
    assert by_call["b"].startswith("<persisted-output>")
    assert by_call["c"] == "small result"
    assert by_call["d"] == "D" * 5000  # protected tail turn
    assert stats.pruned_count == 1
    assert stats.tokens_before == estimate_tokens(messages)
    assert stats.tokens_after == estimate_tokens(replacement)
    assert stats.reclaimed > 0
    # Pairing survives: ai tool_calls are copied verbatim.
    ai_entries = [entry for entry in replacement if entry["role"] == "ai"]
    assert [entry["tool_calls"][0]["id"] for entry in ai_entries] == [
        "a",
        "b",
        "c",
        "d",
    ]


def test_replacement_is_idempotent_on_its_own_output():
    messages, _ = _prune_scenario()
    config = _config()
    replacement, _ = build_pruned_replacement(
        messages, select_window(messages, config), config
    )

    # A second pass over the rebuilt transcript must not re-truncate.
    second, stats = build_pruned_replacement(
        replacement, select_window(replacement, config), config
    )

    assert second == replacement
    assert stats.pruned_count == 0
    assert stats.reclaimed == 0


def test_replacement_drops_projection_synthetic_entries():
    messages = [
        _human("t1"),
        _ai("", [_call("done"), _call("unknown")]),
        _tool("done", "D" * 5000),
        _tool("unknown", "aborted"),
        _human("t2"),
        _ai("final"),
    ]
    config = _config()
    window = select_window(messages, config, protect_ids=frozenset({"unknown"}))

    replacement, stats = build_pruned_replacement(
        messages, window, config, synthetic_ids=frozenset({"unknown"})
    )

    tool_entries = [entry for entry in replacement if entry["role"] == "tool"]
    assert [entry["tool_call_id"] for entry in tool_entries] == ["done"]
    assert tool_entries[0]["content"] == "D" * 5000
    assert stats.tokens_after == estimate_tokens(replacement)
    assert stats.tokens_after < stats.tokens_before


def test_estimate_tokens_follows_the_chars_over_four_formula():
    calls = [_call("a")]
    messages = [_human("q" * 7), _ai("", calls), _tool("a", "x" * 11)]

    expected_content = math.ceil((7 + 0 + 11) / 4)
    expected_calls = math.ceil(len(json.dumps(calls)) / 4)
    assert estimate_tokens(messages) == expected_content + expected_calls

    # The content sum is ceiled once overall (not per message).
    assert estimate_tokens([_human("abcde"), _ai("fghij")]) == 3


# ---------------------------------------------------------------------------
# Trigger chain (§5.2) and status mapping (§2.5)
# ---------------------------------------------------------------------------


@dataclass
class _FakeProjection:
    messages: tuple
    synthetic_tool_call_ids: frozenset = frozenset()
    latest_event_seq: Optional[int] = None
    last_compaction: Any = None


def _big_projection():
    return _FakeProjection(
        messages=(
            _human("first"),
            _ai("", [_call("old")]),
            _tool("old", "B" * 20000),
            _human("current"),
            _ai("answer"),
        ),
        latest_event_seq=42,
    )


def test_trigger_rejects_when_disabled_or_without_a_watermark():
    projection = _big_projection()

    disabled = decide_compaction(
        projection, _config(context_compaction_enabled=False), 200000
    )
    assert disabled.gate == "disabled"
    assert not disabled.should_compact

    no_watermark = decide_compaction(
        projection, _config(proactive_prune_tokens=0), 200000
    )
    assert no_watermark.gate == "no_watermark"
    assert not no_watermark.should_compact


def test_trigger_watermark_gate_and_window_clamp():
    projection = _big_projection()

    rejected = decide_compaction(
        _FakeProjection(messages=(_human("hi"), _ai("ok"))),
        _config(proactive_prune_tokens=200000),
        200000,
    )
    assert rejected.gate == "watermark"
    assert rejected.watermark == 80000  # min(200000, 200000 * 0.4)
    assert not rejected.should_compact

    clamped = decide_compaction(
        projection, _config(proactive_prune_tokens=48000), 32000
    )
    assert clamped.watermark == 12800  # F5: min(48000, 32000 * 0.4)


def test_trigger_rearm_gate_reads_last_compaction():
    projection = _big_projection()
    projection.last_compaction = type(
        "S", (), {"rearm_tokens": 10**9, "through_event_seq": 1, "accounting": {}}
    )()

    decision = decide_compaction(projection, _config(), 200000)

    assert decision.gate == "rearm"
    assert decision.rearm_gate == 10**9


def test_trigger_window_empty_when_everything_is_protected():
    projection = _FakeProjection(messages=(_human("only"), _ai("turn")))

    decision = decide_compaction(
        projection, _config(proactive_prune_tokens=1, protect_last_rounds=3), 200000
    )

    assert decision.gate == "window_empty"
    assert decision.would_prune_count == 0


def test_trigger_reclaim_gate_measures_the_trial_prune():
    projection = _big_projection()

    # A tiny tail budget (current turn only) plus an out-of-reach trigger
    # factor keeps the plain reclaim rejection observable.
    decision = decide_compaction(
        projection,
        _config(
            min_reclaim_tokens=10**9,
            protect_tail_tokens=2,
            pressure_soft_factor=1000,
        ),
        200000,
    )

    assert decision.gate == "reclaim"
    assert decision.would_reclaim > 0
    assert decision.would_prune_count == 1


def test_trigger_commits_a_draft_with_rearm_and_accounting():
    projection = _big_projection()

    decision = decide_compaction(projection, _config(), 200000)

    assert decision.gate == "passed"
    assert decision.should_compact
    draft = decision.draft
    assert draft.through_event_seq == 42
    assert draft.accounting == {
        "tokens_before": decision.est_tokens,
        "tokens_after": decision.est_tokens - decision.would_reclaim,
        "reclaimed": decision.would_reclaim,
        "pruned_count": 1,
        "dedupe_count": 0,
    }
    assert draft.rearm_tokens == draft.accounting["tokens_after"] + max(
        draft.accounting["reclaimed"], decision.watermark, 100
    )
    assert draft.replacement_history[2]["content"] == (
        '[query] args≈{"sql":"select 1"} · success=? · 20000 chars'
    )


def test_status_state_mapping_and_threshold_resolution():
    assert context_status_state(10, 100, 750) == "normal"
    assert context_status_state(100, 100, 750) == "low_watermark_active"
    assert context_status_state(750, 100, 750) == "threshold_reached"

    config = _config()
    assert compact_threshold_tokens_for(config, 100000) == 75000
    assert (
        compact_threshold_tokens_for(_config(compact_threshold_tokens=500), 100000)
        == 500
    )


# ---------------------------------------------------------------------------
# Dedupe (§2.1)
# ---------------------------------------------------------------------------


def _dup_turn(call_id, content):
    return [
        _human(f"turn {call_id}"),
        _ai("", [_call(call_id)]),
        _tool(call_id, content),
    ]


def test_dedupe_replaces_older_copy_with_reference_to_the_kept_one():
    big = "Z" * 3000
    messages = [
        *_dup_turn("a1", big),
        *_dup_turn("a2", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=2, min_prune_chars=1000)
    window = select_window(messages, config)

    entries, replaced = dedupe_replacement(messages, window, config)

    assert replaced == 1
    assert (
        entries[2]["content"]
        == f"{DEDUPE_REFERENCE_PREFIX}query result — content omitted]"
    )
    assert entries[5]["content"] == big


def test_dedupe_keeps_only_the_newest_copy_for_multiple_duplicates():
    big = "Z" * 3000
    messages = [
        *_dup_turn("a1", big),
        *_dup_turn("a2", big),
        *_dup_turn("a3", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=2, min_prune_chars=1000)
    window = select_window(messages, config)

    entries, replaced = dedupe_replacement(messages, window, config)

    assert replaced == 2
    assert entries[2]["content"].startswith(DEDUPE_REFERENCE_PREFIX)
    assert entries[5]["content"].startswith(DEDUPE_REFERENCE_PREFIX)
    assert entries[8]["content"] == big


def test_dedupe_never_touches_copies_inside_protected_turns():
    big = "Z" * 3000
    messages = [
        *_dup_turn("a1", big),
        *_dup_turn("a2", big),
        *_dup_turn("a3", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(
        protect_last_rounds=3, max_tail_rounds_floor=8, min_prune_chars=1000
    )
    window = select_window(messages, config)

    entries, replaced = dedupe_replacement(messages, window, config)

    # a2/a3 sit inside the protected tail: only the oldest copy is rewritten.
    assert replaced == 1
    assert entries[2]["content"].startswith(DEDUPE_REFERENCE_PREFIX)
    assert entries[5]["content"] == big
    assert entries[8]["content"] == big


def test_dedupe_skips_when_the_kept_copy_would_be_pruned():
    big = "Z" * 3000
    messages = [
        *_dup_turn("a1", big),
        *_dup_turn("a2", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=1, min_prune_chars=1000)
    window = select_window(messages, config)

    entries, replaced = dedupe_replacement(messages, window, config)

    # F1: the kept copy lives in the compressible window and is big enough to
    # be pruned -> no reference line may point at it.
    assert replaced == 0
    assert entries[2]["content"] == big
    assert entries[5]["content"] == big
    # Both copies then reach the pruner instead (no lie, no reference).
    pruned, stats = build_pruned_replacement(entries, window, config)
    assert stats.pruned_count == 2
    assert "· 3000 chars" in pruned[2]["content"]
    assert "· 3000 chars" in pruned[5]["content"]


def test_dedupe_ignores_contents_below_the_minimum_length():
    small = "s" * 180  # < dedup_min_chars (200)
    messages = [
        *_dup_turn("a1", small),
        *_dup_turn("a2", small),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=2)

    entries, replaced = dedupe_replacement(
        messages, select_window(messages, config), config
    )

    assert replaced == 0
    assert entries[2]["content"] == small


def test_dedupe_ignores_persisted_output_blocks():
    block = "<persisted-output>\n" + "Z" * 3000 + "\n</persisted-output>"
    messages = [
        *_dup_turn("a1", block),
        *_dup_turn("a2", block),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=2)

    entries, replaced = dedupe_replacement(
        messages, select_window(messages, config), config
    )

    assert replaced == 0
    assert entries[2]["content"] == block


def test_dedupe_is_idempotent_on_its_own_output():
    big = "Z" * 3000
    messages = [
        *_dup_turn("a1", big),
        *_dup_turn("a2", big),
        _human("current"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=2, min_prune_chars=1000)
    entries, replaced = dedupe_replacement(
        messages, select_window(messages, config), config
    )
    assert replaced == 1

    second_entries, second_replaced = dedupe_replacement(
        entries, select_window(entries, config), config
    )

    assert second_replaced == 0
    assert second_entries == entries


def test_decide_compaction_accounts_dedupe_with_prune():
    big = "Z" * 3000
    projection = _FakeProjection(
        messages=(
            _human("t1"),
            _ai("", [_call("a1")]),
            _tool("a1", big),
            _human("t2"),
            _ai("", [_call("a2")]),
            _tool("a2", big),
            _human("current"),
            _ai("done"),
        ),
        latest_event_seq=7,
    )
    config = _config(
        protect_last_rounds=2, min_prune_chars=1000, min_reclaim_tokens=100
    )

    decision = decide_compaction(projection, config, 200000)

    assert decision.gate == "passed"
    assert decision.would_dedupe_count == 1
    assert decision.would_prune_count == 0
    draft = decision.draft
    assert draft.accounting["dedupe_count"] == 1
    assert draft.accounting["pruned_count"] == 0
    assert draft.accounting["reclaimed"] > 0
    assert draft.replacement_history[2]["content"].startswith(
        DEDUPE_REFERENCE_PREFIX
    )
    assert draft.replacement_history[5]["content"] == big


# ---------------------------------------------------------------------------
# Pressure relief (§2.2)
# ---------------------------------------------------------------------------


def test_select_window_reports_tail_tokens():
    messages = []
    for index in range(5):
        messages.append(_human(f"q{index}"))
        messages.append(_ai("x" * 40))

    window = select_window(
        messages, _config(protect_last_rounds=1, protect_tail_tokens=25)
    )

    # Tail scan crossed the 25-token budget with the third tail turn.
    assert window.tail_tokens == 33
    assert window.compressible_indices == frozenset({0, 1, 2, 3})


def test_pressure_relief_triggers_on_deadlock_shaped_transcript():
    big = "P" * 20000
    projection = _FakeProjection(
        messages=(
            _human("t1"),
            _ai("", [_call("a1")]),
            _tool("a1", big),
            _human("current"),
            _ai("done"),
        ),
        latest_event_seq=5,
    )
    config = _config(
        protect_last_rounds=3,
        protect_tail_tokens=1000,
        pressure_soft_factor=1.5,
        min_prune_chars=1000,
        min_reclaim_tokens=100,
    )
    # The normal window is empty: every turn sits inside the tail.
    assert select_window(projection.messages, config).empty

    decision = decide_compaction(projection, config, 200000)

    assert decision.gate == "pressure"
    assert decision.pressure_stage == "A"
    assert decision.should_compact
    assert "· 20000 chars" in decision.draft.replacement_history[2]["content"]


def test_pressure_stage_b_reaches_current_turn_tools_except_the_newest():
    older = "O" * 3000
    current_big = "C" * 20000
    projection = _FakeProjection(
        messages=(
            _human("t1"),
            _ai("", [_call("a1")]),
            _tool("a1", older),
            _human("current"),
            _ai("", [_call("c2"), _call("c3")]),
            _tool("c2", current_big),
            _tool("c3", "small tail"),
        ),
        latest_event_seq=7,
    )
    config = _config(
        protect_last_rounds=3,
        protect_tail_tokens=1000,
        min_prune_chars=1000,
        min_reclaim_tokens=1000,
    )

    decision = decide_compaction(projection, config, 200000)

    # Stage A only clears `older` (< 1000 reclaimed); stage B opens the
    # current turn's big result while sparing the newest tool result.
    assert decision.gate == "pressure"
    assert decision.pressure_stage == "B"
    replacement = decision.draft.replacement_history
    assert "· 20000 chars" in replacement[5]["content"]
    assert replacement[6]["content"] == "small tail"


def test_pressure_stage_c_degrades_the_newest_tool_result_last():
    older = "O" * 3000
    projection = _FakeProjection(
        messages=(
            _human("t1"),
            _ai("", [_call("a1")]),
            _tool("a1", older),
            _human("current"),
            _ai("", [_call("c2")]),
            _tool("c2", "N" * 20000),
        ),
        latest_event_seq=6,
    )
    config = _config(
        protect_last_rounds=3,
        protect_tail_tokens=1000,
        min_prune_chars=1000,
        min_reclaim_tokens=1000,
    )

    decision = decide_compaction(projection, config, 200000)

    # The newest (and only current-turn) tool result is the last resort.
    assert decision.gate == "pressure"
    assert decision.pressure_stage == "C"
    assert "· 20000 chars" in decision.draft.replacement_history[5]["content"]


def test_pressure_respects_hard_lines_even_at_stage_c():
    persisted = "<persisted-output>\n" + "Q" * 5000 + "\n</persisted-output>"
    projection = _FakeProjection(
        messages=(
            _human("t1"),
            _ai("", [_call("done"), _call("unknown")]),
            _tool("done", "D" * 20000),
            _human("t2"),
            _ai("", [_call("p1")]),
            _tool("p1", persisted),
            _human("current"),
            _ai("", [_call("c2")]),
            _tool("c2", "E" * 20000),
        ),
        synthetic_tool_call_ids=frozenset({"unknown"}),
        latest_event_seq=9,
    )
    config = _config(
        protect_last_rounds=3,
        protect_tail_tokens=1000,
        min_prune_chars=1000,
        min_reclaim_tokens=100,
    )

    decision = decide_compaction(projection, config, 200000)

    assert decision.gate == "pressure"
    assert decision.pressure_stage == "C"
    replacement = decision.draft.replacement_history
    # Hard lines: the active user message, the synthetic turn's big result
    # and the persisted pointer block all survive untouched.
    assert replacement[6]["content"] == "current"
    assert replacement[2]["content"] == "D" * 20000
    assert replacement[5]["content"] == persisted
    # Only the plain current-turn result is degraded.
    assert "· 20000 chars" in replacement[8]["content"]


def test_pressure_still_requires_the_reclaim_gate():
    projection = _FakeProjection(
        messages=(
            _human("t1"),
            _ai("", [_call("a1")]),
            _tool("a1", "P" * 20000),
            _human("current"),
            _ai("done"),
        ),
        latest_event_seq=5,
    )
    config = _config(
        protect_last_rounds=3,
        protect_tail_tokens=1000,
        min_prune_chars=1000,
        min_reclaim_tokens=10**9,
    )

    decision = decide_compaction(projection, config, 200000)

    assert decision.gate == "pressure"
    # Even on rejection the best trial's stage is kept for the audit log.
    assert decision.pressure_stage == "A"
    assert not decision.should_compact
    assert decision.would_reclaim > 0


# ---------------------------------------------------------------------------
# One-line prune format (§2.3)
# ---------------------------------------------------------------------------


def test_one_line_format_uses_name_args_success_and_size():
    big = "Z" * 3000
    messages = [
        _human("t1"),
        _ai("", [_call("a1")]),
        _tool("a1", big),
        _human("cur"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=1, min_prune_chars=1000)
    window = select_window(messages, config)

    replacement, stats = build_pruned_replacement(
        messages, window, config, tool_success={"a1": True}
    )

    assert stats.pruned_count == 1
    assert replacement[2]["content"] == (
        '[query] args≈{"sql":"select 1"} · success=true · 3000 chars'
    )


def test_one_line_marks_missing_signals_with_question_marks():
    big = "Z" * 3000
    # No matching assistant tool_call -> name/arguments unknown; no success
    # signal in the projection.
    messages = [_human("t1"), _tool("ghost", big), _human("cur"), _ai("done")]
    config = _config(protect_last_rounds=1, min_prune_chars=1000)
    window = select_window(messages, config)

    replacement, stats = build_pruned_replacement(
        messages, window, config, tool_success={"ghost": False}
    )

    assert stats.pruned_count == 1
    assert replacement[1]["content"] == "[tool] args≈? · success=false · 3000 chars"


def test_one_line_replaces_argument_newlines_and_caps_the_preview():
    big = "Z" * 3000
    call = {
        "id": "a1",
        "type": "function",
        "function": {
            "name": "query",
            "arguments": '{"a": 1,\n "b": 2,\n "c": "' + "x" * 200 + '"}',
        },
    }
    messages = [
        _human("t1"),
        _ai("", [call]),
        _tool("a1", big),
        _human("cur"),
        _ai("done"),
    ]
    config = _config(
        protect_last_rounds=1, min_prune_chars=1000, prune_args_preview_chars=80
    )
    window = select_window(messages, config)

    replacement, stats = build_pruned_replacement(messages, window, config)

    line = replacement[2]["content"]
    assert stats.pruned_count == 1
    assert "\n" not in line
    # Newlines become spaces (kept verbatim otherwise), preview capped at 80.
    expected_preview = call["function"]["arguments"].replace("\n", " ")[:80]
    assert line == f"[query] args≈{expected_preview} · success=? · 3000 chars"
    assert len(expected_preview) == 80


def test_one_line_net_gain_gate_keeps_small_results():
    content = "Z" * 300  # over min_prune_chars (200) but the gain is tiny
    messages = [
        _human("t1"),
        _ai("", [_call("a1")]),
        _tool("a1", content),
        _human("cur"),
        _ai("done"),
    ]
    config = _config(protect_last_rounds=1, min_prune_chars=200)
    window = select_window(messages, config)

    replacement, stats = build_pruned_replacement(messages, window, config)

    # 300 - ~58 < min_saving_chars (500): the result stays as-is.
    assert stats.pruned_count == 0
    assert replacement[2]["content"] == content


def test_already_processed_contents_are_never_rewritten():
    already = [
        '[query] args≈{"sql":"select 1"} · success=? · 3000 chars',
        "Z" * 1000 + " … [truncated, total 3000 chars]",
        "[duplicate of the later query result — content omitted]",
    ]
    messages = []
    for index, content in enumerate(already):
        messages.extend(
            [
                _human(f"t{index}"),
                _ai("", [_call(f"c{index}")]),
                _tool(f"c{index}", content),
            ]
        )
    messages.extend([_human("cur"), _ai("done")])
    config = _config(protect_last_rounds=1, min_prune_chars=1000)
    window = select_window(messages, config)

    replacement, stats = build_pruned_replacement(messages, window, config)

    assert stats.pruned_count == 0
    rewritten = [
        replacement[2]["content"],
        replacement[5]["content"],
        replacement[8]["content"],
    ]
    assert rewritten == already
