"""Structural compaction planning for rollout transcripts (Phase 0 + 1/2).

Pure functions only: turn detection, window selection, dedupe, replacement
building and the trigger decision. No storage or event IO happens here; the
rollout coordinator (``dbgpt_serve.agent.db.rollout_compaction``) wires these
to the append-only event log and the turn lease.

Everything operates on projected ``ModelMessage`` items (role
``human``/``ai``/``tool``) as produced by ``dbgpt.agent.core.rollout``:

- turns start at a plain human message and own every ai/tool message up to
  the next human start;
- orphan segments (ai/tool before any human), turns containing unresolved
  (synthetic) calls and summary-marked segments are never pruned;
- the newest turns are always protected (tail window), and the current turn
  is protected by construction (it is the newest human turn).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from dbgpt.core.interface.message import ModelMessageRoleType

from .budget import DEFAULT_MAX_CONTEXT_TOKENS, ContextBudgetConfig
from .storage import PERSISTED_OUTPUT_TAG

# In-content marker of a compaction summary message (aligned with the legacy
# L3 prefix in compact.py). Summary segments are never turn starts and are
# never pruned again.
SUMMARY_MARKER = "[Context Summary of"

# The low watermark is clamped to this fraction of the model's context window
# (F5): on a 32K-token window a configured 48K watermark would fire once and
# then never re-arm.
WATERMARK_WINDOW_FRACTION = 0.4

# Floor for the truncation threshold ("clamp>=200" keeps summaries from being
# re-summarized).
_MIN_PRUNE_CHARS = 200

_TRUNCATION_MARKER_RE = re.compile(r"… \[truncated, total \d+ chars\]\s*$")
# Aggressive one-line prune format (§2.3); the regex doubles as the
# idempotency marker so a one-lined result is never processed twice.
_ONE_LINE_MARKER_RE = re.compile(
    r"^\s*\[[^\]]+\]\s+args≈.*·\s+success=(?:true|false|\?)\s+·\s+\d+ chars\s*$"
)
# Reference line replacing an older byte-identical tool result (§2.1). The
# prefix doubles as the idempotency marker.
DEDUPE_REFERENCE_PREFIX = "[duplicate of the later "


@dataclass(frozen=True)
class Turn:
    """One conversational turn of the projected transcript."""

    start: int
    indices: Tuple[int, ...]
    is_orphan: bool = False


@dataclass(frozen=True)
class CompactionWindow:
    """Turns of the transcript plus the indices open to pruning.

    ``tail_tokens`` is the estimated size of the protected tail turns (F3);
    the pressure relief trigger compares it against the soft budget.
    """

    turns: Tuple[Turn, ...]
    compressible_indices: frozenset[int]
    tail_tokens: int = 0

    @property
    def empty(self) -> bool:
        return not self.compressible_indices


@dataclass(frozen=True)
class PruneStats:
    """Estimated token accounting of one replacement build."""

    tokens_before: int
    tokens_after: int
    pruned_count: int

    @property
    def reclaimed(self) -> int:
        return self.tokens_before - self.tokens_after


@dataclass(frozen=True)
class CompactionDraft:
    """A decided compaction, ready to be written as a context_compacted event."""

    replacement_history: list[dict]
    rearm_tokens: int
    through_event_seq: Optional[int]
    accounting: dict


@dataclass(frozen=True)
class CompactionDecision:
    """Trigger-chain outcome (including the blocking gate, for observability)."""

    gate: str
    est_tokens: int
    watermark: int
    rearm_gate: int
    would_reclaim: int
    would_prune_count: int
    would_dedupe_count: int = 0
    pressure_stage: Optional[str] = None
    draft: Optional[CompactionDraft] = None

    @property
    def should_compact(self) -> bool:
        return self.draft is not None


def _role_of(message: Any) -> str:
    if isinstance(message, Mapping):
        role = message.get("role")
    else:
        role = getattr(message, "role", None)
    return role if isinstance(role, str) else ""


def _content_of(message: Any) -> str:
    if isinstance(message, Mapping):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


def _tool_calls_of(message: Any) -> Optional[list]:
    if isinstance(message, Mapping):
        calls = message.get("tool_calls")
    else:
        calls = getattr(message, "tool_calls", None)
    return calls or None


def _tool_call_id_of(message: Any) -> Optional[str]:
    if isinstance(message, Mapping):
        call_id = message.get("tool_call_id")
    else:
        call_id = getattr(message, "tool_call_id", None)
    return call_id if isinstance(call_id, str) else None


def _tool_call_info_map(messages: Sequence[Any]) -> dict[str, Tuple[str, str]]:
    """Map tool_call id -> (name, arguments) from assistant messages."""
    info: dict[str, Tuple[str, str]] = {}
    for message in messages:
        for tool_call in _tool_calls_of(message) or []:
            if not isinstance(tool_call, Mapping):
                continue
            call_id = tool_call.get("id")
            function = tool_call.get("function")
            if not isinstance(call_id, str) or not call_id:
                continue
            name = ""
            arguments = ""
            if isinstance(function, Mapping):
                raw_name = function.get("name")
                raw_args = function.get("arguments")
                name = raw_name if isinstance(raw_name, str) else ""
                arguments = raw_args if isinstance(raw_args, str) else ""
            info[call_id] = (name, arguments)
    return info


def _is_processed_tool_content(content: str) -> bool:
    """True when a tool content already carries one of the producer markers.

    Recognized markers (§2.3): the one-line prune format, the Phase 0
    truncation suffix and the dedupe reference prefix. Unmarked content is
    eligible for pruning and dedupe.
    """
    return bool(
        _ONE_LINE_MARKER_RE.search(content)
        or _TRUNCATION_MARKER_RE.search(content)
        or content.startswith(DEDUPE_REFERENCE_PREFIX)
    )


def render_one_line(
    content_length: int,
    call_id: Optional[str],
    call_info: Mapping[str, Tuple[str, str]],
    tool_success: Mapping[str, bool],
    config: ContextBudgetConfig,
) -> str:
    """Render the aggressive one-line prune replacement (§2.3).

    ``[{tool_name}] args≈{arguments preview} · success={true|false|?} · {N} chars``
    — the name/arguments come from the same turn's tool_calls map, ``success``
    from the projection's structured signal, and ``N`` is the replaced
    content's original character count.
    """
    name, arguments = call_info.get(call_id, ("", ""))
    name = name or "tool"
    preview = "?"
    if arguments:
        preview = arguments.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        preview = preview[: max(0, int(config.prune_args_preview_chars))]
    success = tool_success.get(call_id)
    if success is True:
        success_text = "true"
    elif success is False:
        success_text = "false"
    else:
        success_text = "?"
    return f"[{name}] args≈{preview} · success={success_text} · {content_length} chars"


def estimate_tokens(messages: Iterable[Any]) -> int:
    """Estimated tokens of a projected transcript (chars/4, F12 decision).

    ``ceil(sum(len(content)) / 4) + sum(ceil(len(json.dumps(tool_calls)) / 4))``
    over the projection message bodies only — no system prompt or tool-schema
    overhead. The same figure feeds gating, accounting and context.status.
    """
    content_chars = 0
    call_chars = 0
    for message in messages:
        content_chars += len(_content_of(message))
        tool_calls = _tool_calls_of(message)
        if tool_calls:
            call_chars += math.ceil(len(json.dumps(tool_calls)) / 4)
    return math.ceil(content_chars / 4) + call_chars


def is_summary_message(message: Any) -> bool:
    """True when a human message carries the compaction-summary marker."""
    return _content_of(message).startswith(SUMMARY_MARKER)


def detect_turns(messages: Sequence[Any]) -> list[Turn]:
    """Group projected messages into turns (§3.2).

    System/view messages do not participate and keep their positions. Ai/tool
    messages before any human start form one orphan segment that is marked
    ``is_orphan`` and never pruned.
    """
    turns: list[Turn] = []
    current: Optional[list[int]] = None
    current_orphan = False

    for index, message in enumerate(messages):
        role = _role_of(message)
        if role in (ModelMessageRoleType.SYSTEM, ModelMessageRoleType.VIEW):
            continue
        if role == ModelMessageRoleType.HUMAN and not is_summary_message(message):
            if current is not None:
                turns.append(
                    Turn(
                        start=current[0],
                        indices=tuple(current),
                        is_orphan=current_orphan,
                    )
                )
            current = [index]
            current_orphan = False
        else:
            if current is None:
                current = [index]
                current_orphan = True
            else:
                current.append(index)

    if current is not None:
        turns.append(
            Turn(start=current[0], indices=tuple(current), is_orphan=current_orphan)
        )
    return turns


def _turn_tokens(messages: Sequence[Any], turn: Turn) -> int:
    return estimate_tokens(messages[index] for index in turn.indices)


def _turn_has_protected_calls(
    messages: Sequence[Any], turn: Turn, protect_ids: frozenset[str]
) -> bool:
    if not protect_ids:
        return False
    for index in turn.indices:
        message = messages[index]
        if _tool_call_id_of(message) in protect_ids:
            return True
        for tool_call in _tool_calls_of(message) or []:
            if isinstance(tool_call, Mapping) and tool_call.get("id") in protect_ids:
                return True
    return False


def _turn_has_summary_marker(messages: Sequence[Any], turn: Turn) -> bool:
    return any(is_summary_message(messages[index]) for index in turn.indices)


def select_window(
    messages: Sequence[Any],
    config: ContextBudgetConfig,
    protect_ids: frozenset[str] = frozenset(),
    pressure: bool = False,
    *,
    pressure_stage: str = "A",
) -> CompactionWindow:
    """Choose which message indices may be pruned (§3.4, §2.2).

    The newest turns stay protected: at least ``protect_last_rounds`` turns,
    extended while their estimated size is below ``protect_tail_tokens``, and
    capped at ``max_tail_rounds_floor`` turns to avoid the "nothing left to
    prune" deadlock when big outputs fill the tail. Turns containing
    unresolved (synthetic) calls, orphan segments and summary segments are
    additionally protected; every other turn is compressible.

    ``pressure=True`` widens the window into the tail (Pass 4), stage by
    stage (§2.2): "A" opens the tail's tool results outside the current turn,
    "B" every tail tool result except the newest one, "C" all of them. The
    hard line holds in every stage: only tool contents are ever degraded,
    persisted blocks are skipped by the pruner, and orphan / synthetic turns
    keep their protection (known limitation: a deadlock living inside such a
    turn cannot be relieved).
    """
    turns = detect_turns(messages)
    protect_tail_tokens = max(0, int(config.protect_tail_tokens))
    protect_last_rounds = max(1, int(config.protect_last_rounds))
    max_tail_turns = max(protect_last_rounds, int(config.max_tail_rounds_floor))

    tail_count = 0
    accumulated = 0
    for turn in reversed(turns):
        if tail_count >= max_tail_turns:
            break
        if tail_count >= protect_last_rounds and accumulated >= protect_tail_tokens:
            break
        accumulated += _turn_tokens(messages, turn)
        tail_count += 1
    tail_positions = set(range(len(turns) - tail_count, len(turns)))

    compressible: set[int] = set()
    for position, turn in enumerate(turns):
        if position in tail_positions:
            continue
        if turn.is_orphan:
            continue
        if _turn_has_summary_marker(messages, turn):
            continue
        if _turn_has_protected_calls(messages, turn, protect_ids):
            continue
        compressible.update(turn.indices)

    if pressure and turns:
        stage = (pressure_stage or "A").upper()
        if stage not in ("A", "B", "C"):
            raise ValueError(f"unknown pressure stage: {pressure_stage!r}")
        current_turn = turns[-1]
        tail_tool_indices: list[int] = []
        for position in sorted(tail_positions):
            turn = turns[position]
            if turn.is_orphan:
                continue
            if _turn_has_summary_marker(messages, turn):
                continue
            if _turn_has_protected_calls(messages, turn, protect_ids):
                continue
            tail_tool_indices.extend(
                index
                for index in turn.indices
                if _role_of(messages[index]) == ModelMessageRoleType.TOOL
            )
        if stage == "A":
            tail_tool_indices = [
                index
                for index in tail_tool_indices
                if index not in current_turn.indices
            ]
        elif stage == "B":
            newest_tool = max(tail_tool_indices) if tail_tool_indices else None
            tail_tool_indices = [
                index for index in tail_tool_indices if index != newest_tool
            ]
        compressible.update(tail_tool_indices)

    return CompactionWindow(
        turns=tuple(turns),
        compressible_indices=frozenset(compressible),
        tail_tokens=accumulated,
    )


def build_pruned_replacement(
    messages: Sequence[Any],
    window: CompactionWindow,
    config: ContextBudgetConfig,
    synthetic_ids: frozenset[str] = frozenset(),
    tool_success: Mapping[str, bool] = {},
) -> Tuple[list[dict], PruneStats]:
    """Rebuild the full transcript with old tool results pruned (§2.3/§3.6).

    - projection-only synthetic ("aborted") entries are dropped (I2): the
      projector re-synthesizes them from the replacement's open calls;
    - ``<persisted-output>`` blocks are kept verbatim (the file path lives in
      the block itself);
    - other over-threshold tool results inside the compressible window are
      replaced by the aggressive one line (§2.3) when the change clears the
      net-gain gate (``min_saving_chars``); otherwise they stay untouched;
    - already-processed contents (one-line / Phase 0 truncation / dedupe
      reference) are never rewritten twice (idempotency).
    """
    threshold = max(_MIN_PRUNE_CHARS, int(config.min_prune_chars))
    min_saving = max(0, int(config.min_saving_chars))
    call_info = _tool_call_info_map(messages)
    tokens_before = estimate_tokens(messages)
    replacement: list[dict] = []
    pruned_count = 0

    for index, message in enumerate(messages):
        role = _role_of(message)
        content = _content_of(message)
        if role == ModelMessageRoleType.HUMAN:
            replacement.append({"role": "human", "content": content})
            continue
        if role == ModelMessageRoleType.AI:
            entry: dict[str, Any] = {"role": "ai", "content": content}
            tool_calls = _tool_calls_of(message)
            if tool_calls:
                entry["tool_calls"] = deepcopy(tool_calls)
            replacement.append(entry)
            continue
        if role != ModelMessageRoleType.TOOL:
            raise ValueError(f"unexpected role in projection: {role!r}")

        call_id = _tool_call_id_of(message)
        if call_id is not None and call_id in synthetic_ids:
            continue
        if (
            index in window.compressible_indices
            and PERSISTED_OUTPUT_TAG not in content
            and len(content) > threshold
            and not _is_processed_tool_content(content)
        ):
            one_line = render_one_line(
                len(content), call_id, call_info, tool_success, config
            )
            if len(content) - len(one_line) >= min_saving:
                content = one_line
                pruned_count += 1
        replacement.append(
            {"role": "tool", "tool_call_id": call_id, "content": content}
        )

    stats = PruneStats(
        tokens_before=tokens_before,
        tokens_after=estimate_tokens(replacement),
        pruned_count=pruned_count,
    )
    return replacement, stats


def dedupe_replacement(
    messages: Sequence[Any],
    window: CompactionWindow,
    config: ContextBudgetConfig,
    synthetic_ids: frozenset[str] = frozenset(),
) -> Tuple[list[dict], int]:
    """Replace older byte-identical tool results with a reference line (§2.1).

    Fully lossless: copies are matched by md5 of their exact content, the
    newest copy always stays verbatim, and an older copy is only rewritten
    when it sits inside the compressible window *and* the kept copy cannot be
    pruned later (F1) — a reference into pruned content would lie. Everything
    else is copied through unchanged; the output feeds
    :func:`build_pruned_replacement` so dedupe and pruning share one
    accounting.
    """
    min_chars = max(1, int(config.dedup_min_chars))
    prune_threshold = max(_MIN_PRUNE_CHARS, int(config.min_prune_chars))
    call_info = _tool_call_info_map(messages)

    def _candidate_content(index: int) -> Optional[str]:
        message = messages[index]
        if _role_of(message) != ModelMessageRoleType.TOOL:
            return None
        call_id = _tool_call_id_of(message)
        if call_id is not None and call_id in synthetic_ids:
            return None
        content = _content_of(message)
        if len(content) < min_chars:
            return None
        # Pointer semantics: persisted blocks never participate (F8).
        if PERSISTED_OUTPUT_TAG in content:
            return None
        if _is_processed_tool_content(content):
            return None
        return content

    # Newest-first scan: the newest eligible copy of a digest is the kept one.
    kept_copy: dict[str, int] = {}
    for index in range(len(messages) - 1, -1, -1):
        content = _candidate_content(index)
        if content is None:
            continue
        digest = hashlib.md5(content.encode("utf-8")).hexdigest()
        kept_copy.setdefault(digest, index)

    def _kept_will_survive(index: int) -> bool:
        # F1: only reference a keep-copy that pruning will not touch. A copy
        # that survives via protection, via being below the prune threshold,
        # or via being a persisted block is safe to point at.
        message = messages[index]
        content = _content_of(message)
        if PERSISTED_OUTPUT_TAG in content:
            return True
        if index not in window.compressible_indices:
            return True
        return len(content) <= prune_threshold

    entries: list[dict] = []
    replaced = 0
    for index, message in enumerate(messages):
        role = _role_of(message)
        content = _content_of(message)
        if role == ModelMessageRoleType.TOOL:
            candidate = _candidate_content(index)
            if candidate is not None:
                digest = hashlib.md5(candidate.encode("utf-8")).hexdigest()
                kept_index = kept_copy.get(digest)
                if (
                    kept_index is not None
                    and kept_index != index
                    and index in window.compressible_indices
                    and _kept_will_survive(kept_index)
                ):
                    tool_name = (
                        call_info.get(_tool_call_id_of(message), ("", ""))[0] or "tool"
                    )
                    entries.append(
                        {
                            "role": "tool",
                            "tool_call_id": _tool_call_id_of(message),
                            "content": (
                                f"{DEDUPE_REFERENCE_PREFIX}{tool_name} result "
                                "— content omitted]"
                            ),
                        }
                    )
                    replaced += 1
                    continue
            entries.append(
                {
                    "role": "tool",
                    "tool_call_id": _tool_call_id_of(message),
                    "content": content,
                }
            )
            continue
        if role == ModelMessageRoleType.AI:
            entry: dict[str, Any] = {"role": "ai", "content": content}
            tool_calls = _tool_calls_of(message)
            if tool_calls:
                entry["tool_calls"] = deepcopy(tool_calls)
            entries.append(entry)
            continue
        if role == ModelMessageRoleType.HUMAN:
            entries.append({"role": "human", "content": content})
            continue
        raise ValueError(f"unexpected role in projection: {role!r}")

    return entries, replaced


def effective_window_tokens(
    config: ContextBudgetConfig, window_tokens: Optional[int]
) -> int:
    """Resolve the window used for watermark clamping and status ratios."""
    if window_tokens and window_tokens > 0:
        return int(window_tokens)
    return int(config.max_context_tokens or DEFAULT_MAX_CONTEXT_TOKENS)


def compact_threshold_tokens_for(
    config: ContextBudgetConfig, window_tokens: int
) -> int:
    """Threshold separating low-watermark maintenance from the compaction域.

    Phase 0 only labels ``context.status`` with it; the Phase 2 summary
    producer consumes it for triggering.
    """
    if config.compact_threshold_tokens and config.compact_threshold_tokens > 0:
        return int(config.compact_threshold_tokens)
    ratio = (
        config.compact_threshold_ratio if config.compact_threshold_ratio > 0 else 0.75
    )
    return int(window_tokens * ratio)


def context_status_state(
    est_tokens: int, watermark: int, threshold_tokens: int
) -> str:
    """§2.5 state mapping for the context.status channel."""
    if est_tokens >= threshold_tokens:
        return "threshold_reached"
    if est_tokens >= watermark:
        return "low_watermark_active"
    return "normal"


def decide_compaction(
    projection: Any,
    config: ContextBudgetConfig,
    window_tokens: Optional[int] = None,
) -> CompactionDecision:
    """Run the §5.2 trigger chain against a projection.

    Returns a :class:`CompactionDecision` carrying the blocking gate, the
    watermark and (when the window selection and trial prune ran) the trial
    accounting. ``decision.draft`` is only set when every gate passed.
    """
    window = effective_window_tokens(config, window_tokens)
    est = estimate_tokens(projection.messages)

    def rejected(
        gate: str, watermark: int = 0, rearm_gate: int = 0
    ) -> CompactionDecision:
        return CompactionDecision(
            gate=gate,
            est_tokens=est,
            watermark=watermark,
            rearm_gate=rearm_gate,
            would_reclaim=0,
            would_prune_count=0,
        )

    if not config.context_compaction_enabled:
        return rejected("disabled")
    if config.proactive_prune_tokens <= 0:
        return rejected("no_watermark")

    watermark = min(
        int(config.proactive_prune_tokens),
        int(window * WATERMARK_WINDOW_FRACTION),
    )
    last_compaction = getattr(projection, "last_compaction", None)
    rearm_gate = (
        int(last_compaction.rearm_tokens) if last_compaction is not None else 0
    )
    if est < max(watermark, rearm_gate):
        gate = "rearm" if rearm_gate > watermark else "watermark"
        return rejected(gate, watermark=watermark, rearm_gate=rearm_gate)

    min_reclaim = int(config.min_reclaim_tokens)
    tool_success = getattr(projection, "tool_success", None) or {}

    def attempt(
        compaction_window: CompactionWindow,
    ) -> Optional[Tuple[list[dict], PruneStats, int]]:
        """Trial dedupe+prune for one window (None when nothing to do)."""
        if compaction_window.empty:
            return None
        deduped, dedupe_count = dedupe_replacement(
            projection.messages,
            compaction_window,
            config,
            synthetic_ids=projection.synthetic_tool_call_ids,
        )
        replacement, stats = build_pruned_replacement(
            deduped,
            compaction_window,
            config,
            synthetic_ids=projection.synthetic_tool_call_ids,
            tool_success=tool_success,
        )
        return replacement, stats, dedupe_count

    def committed(
        replacement: list[dict],
        stats: PruneStats,
        dedupe_count: int,
        reclaimed: int,
        gate: str,
        pressure_stage: Optional[str] = None,
    ) -> CompactionDecision:
        rearm = stats.tokens_after + max(reclaimed, watermark, min_reclaim)
        accounting = {
            "tokens_before": est,
            "tokens_after": stats.tokens_after,
            "reclaimed": reclaimed,
            "pruned_count": stats.pruned_count,
            "dedupe_count": dedupe_count,
        }
        draft = CompactionDraft(
            replacement_history=replacement,
            rearm_tokens=rearm,
            through_event_seq=getattr(projection, "latest_event_seq", None),
            accounting=accounting,
        )
        return CompactionDecision(
            gate=gate,
            est_tokens=est,
            watermark=watermark,
            rearm_gate=rearm_gate,
            would_reclaim=reclaimed,
            would_prune_count=stats.pruned_count,
            would_dedupe_count=dedupe_count,
            pressure_stage=pressure_stage,
            draft=draft,
        )

    compaction_window = select_window(
        projection.messages, config, protect_ids=projection.synthetic_tool_call_ids
    )
    normal_reclaim = 0
    normal_pruned = 0
    normal_deduped = 0
    normal_gate = "reclaim"
    trial = attempt(compaction_window)
    if trial is None:
        normal_gate = "window_empty"
    else:
        replacement, stats, dedupe_count = trial
        normal_reclaim = est - stats.tokens_after
        normal_pruned = stats.pruned_count
        normal_deduped = dedupe_count
        if normal_reclaim >= min_reclaim:
            return committed(
                replacement, stats, dedupe_count, normal_reclaim, gate="passed"
            )

    # Pressure relief (Pass 4, §2.2): only once the protected tail outgrows
    # the soft budget, and only while the normal window came up short.
    soft_budget = int(config.protect_tail_tokens * config.pressure_soft_factor)
    if compaction_window.tail_tokens > soft_budget:
        best: Optional[Tuple[int, int, int, list[dict], PruneStats]] = None
        any_stage = False
        for stage in ("A", "B", "C"):
            stage_window = select_window(
                projection.messages,
                config,
                protect_ids=projection.synthetic_tool_call_ids,
                pressure=True,
                pressure_stage=stage,
            )
            stage_trial = attempt(stage_window)
            if stage_trial is None:
                continue
            any_stage = True
            s_replacement, s_stats, s_dedupe = stage_trial
            s_reclaimed = est - s_stats.tokens_after
            if best is None or s_reclaimed > best[0]:
                best = (
                    s_reclaimed,
                    s_stats.pruned_count,
                    s_dedupe,
                    s_replacement,
                    s_stats,
                )
            if s_reclaimed >= min_reclaim:
                return committed(
                    s_replacement,
                    s_stats,
                    s_dedupe,
                    s_reclaimed,
                    gate="pressure",
                    pressure_stage=stage,
                )
        if any_stage and best is not None:
            # Relief ran but could not clear the reclaim gate either.
            return CompactionDecision(
                gate="pressure",
                est_tokens=est,
                watermark=watermark,
                rearm_gate=rearm_gate,
                would_reclaim=best[0],
                would_prune_count=best[1],
                would_dedupe_count=best[2],
            )

    return CompactionDecision(
        gate=normal_gate,
        est_tokens=est,
        watermark=watermark,
        rearm_gate=rearm_gate,
        would_reclaim=normal_reclaim,
        would_prune_count=normal_pruned,
        would_dedupe_count=normal_deduped,
    )
