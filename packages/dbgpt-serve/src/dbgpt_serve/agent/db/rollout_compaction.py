"""Coordinator wiring rollout-transcript compaction to the event log.

Pure decision logic lives in
``dbgpt.agent.core.context.transcript_compaction``; this module connects it to
the append-only, lease-fenced event stream and produces the ``context.status``
payload for the SSE layer.

Contract (Phase 0 design, §2.3/§5.2):

- the commit point is the ``context_compacted`` append; a failed append means
  "nothing happened" (the transcript is unchanged and the next step retries);
- dry-run runs the full pipeline (gates + trial prune + reclaim measurement)
  and emits one structured log line instead of writing an event;
- recoverable failures never propagate: compaction is a hardening layer and
  must not take the main request down.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from starlette.concurrency import run_in_threadpool

from dbgpt.agent.core.context.budget import ContextBudgetConfig
from dbgpt.agent.core.context.transcript_compaction import (
    CompactionDecision,
    SummaryDecision,
    assemble_summary_draft,
    compact_threshold_tokens_for,
    context_status_state,
    decide_compaction,
    decide_summary,
    effective_window_tokens,
)
from dbgpt.agent.core.context.transcript_summary import (
    build_summary_message_content,
    summarize_region,
    summary_template_name,
)
from dbgpt.agent.core.rollout import ChatCompletionHistoryProjection

from .agent_event_db import AgentRolloutSession, StoredAgentEvent

logger = logging.getLogger(__name__)

COMPACTION_LOG_PREFIX = "context_compaction"


class SummaryFailureTracker:
    """Per-conversation summary failure counter and cooldown (§3.6).

    Deliberately in-memory: a process restart resets the state (accepted
    trade-off). The cooldown only suppresses summary attempts; pruning is
    never affected.
    """

    def __init__(self) -> None:
        self._failures: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}

    def in_cooldown(self, conv_id: str, now: Optional[float] = None) -> bool:
        until = self._cooldown_until.get(conv_id)
        if until is None:
            return False
        current = time.monotonic() if now is None else now
        if current >= until:
            self._cooldown_until.pop(conv_id, None)
            return False
        return True

    def record_failure(
        self, conv_id: str, config: ContextBudgetConfig, now: Optional[float] = None
    ) -> None:
        current = time.monotonic() if now is None else now
        count = self._failures.get(conv_id, 0) + 1
        if count >= max(1, int(config.summary_failure_threshold)):
            cooldown = max(0.0, float(config.summary_cooldown_seconds))
            if cooldown > 0:
                self._cooldown_until[conv_id] = current + cooldown
            self._failures[conv_id] = 0
        else:
            self._failures[conv_id] = count

    def record_success(self, conv_id: str) -> None:
        self._failures.pop(conv_id, None)
        self._cooldown_until.pop(conv_id, None)

    def reset(self, conv_id: Optional[str] = None) -> None:
        """Clear tracker state (tests / operations)."""
        if conv_id is None:
            self._failures.clear()
            self._cooldown_until.clear()
        else:
            self._failures.pop(conv_id, None)
            self._cooldown_until.pop(conv_id, None)


_summary_failures = SummaryFailureTracker()


@dataclass(frozen=True)
class CompactionOutcome:
    """Result of one coordinator run.

    ``status`` is the context.status payload to emit (``None`` when the
    feature is disabled); ``appended`` is set only when a compaction event
    was actually committed; ``decision`` carries gate/accounting details
    (``None`` when evaluation itself failed); ``projection`` is the
    already-loaded (post-commit when one happened) transcript so the caller
    does not need a second read on the normal path.
    """

    status: Optional[Dict[str, Any]]
    decision: Optional[CompactionDecision]
    appended: Optional[StoredAgentEvent]
    dry_run: bool
    projection: Optional[ChatCompletionHistoryProjection] = None


def _status_payload(
    decision: CompactionDecision,
    config: ContextBudgetConfig,
    window_tokens: Optional[int],
) -> Dict[str, Any]:
    window = effective_window_tokens(config, window_tokens)
    threshold = compact_threshold_tokens_for(config, window)
    state = context_status_state(decision.est_tokens, decision.watermark, threshold)
    ratio = round(decision.est_tokens / window, 4) if window > 0 else 1.0
    return {
        "type": "context.status",
        "used": decision.est_tokens,
        "budget": window,
        "ratio": ratio,
        "state": state,
        "compact_layer": None,
    }


def _log_event(event: str, decision: CompactionDecision, **extra: Any) -> None:
    fields = {
        "event": event,
        "gate": decision.gate,
        "est_tokens": decision.est_tokens,
        "watermark": decision.watermark,
        "rearm_gate": decision.rearm_gate,
        "would_reclaim": decision.would_reclaim,
        "would_prune_count": decision.would_prune_count,
        "would_dedupe_count": decision.would_dedupe_count,
        "pressure_stage": decision.pressure_stage,
    }
    fields.update(extra)
    logger.info(
        "%s %s",
        COMPACTION_LOG_PREFIX,
        json.dumps(fields, ensure_ascii=False, sort_keys=True),
    )


def _build_payload(
    draft: Any,
    turn_id: str,
    now: datetime,
    *,
    reason: str = "proactive",
    strategy: str = "prune",
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "reason": reason,
        "strategy": strategy,
        "replacement_history": draft.replacement_history,
        "covers": {"through_event_seq": draft.through_event_seq},
        "accounting": draft.accounting,
        "rearm_tokens": draft.rearm_tokens,
        "created_by": {
            "turn_id": turn_id,
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def _append_payload(
    session: AgentRolloutSession, payload: Dict[str, Any]
) -> Optional[StoredAgentEvent]:
    """Commit one compaction event; None means "nothing happened" (no change)."""
    try:
        return session.append("context_compacted", payload)
    except Exception:
        # The append is the only commit point: on any failure (e.g. a lost
        # turn lease) nothing changed and the next step retries.
        logger.warning(
            "%s append failed; leaving transcript unchanged",
            COMPACTION_LOG_PREFIX,
            exc_info=True,
        )
        return None


def _log_summary_dry_run(
    decision: SummaryDecision, config: ContextBudgetConfig
) -> None:
    fields = {
        "event": "dry_run",
        "strategy": "summary",
        "gate": decision.gate,
        "est_tokens": decision.est_tokens,
        "threshold": decision.threshold,
        "region_tokens": decision.region_tokens,
        "assumed_reclaim": decision.assumed_reclaim,
        "would_template": summary_template_name(config),
    }
    logger.info(
        "%s %s",
        COMPACTION_LOG_PREFIX,
        json.dumps(fields, ensure_ascii=False, sort_keys=True),
    )


def maybe_compact(
    session: AgentRolloutSession,
    config: ContextBudgetConfig,
    *,
    window_tokens: Optional[int] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> CompactionOutcome:
    """Run one compaction step for the session's committed transcript.

    Synchronous (the caller runs it in a worker thread next to the other
    event-log operations). Never raises for recoverable conditions.
    """
    if not config.context_compaction_enabled:
        return CompactionOutcome(
            status=None, decision=None, appended=None, dry_run=dry_run
        )

    try:
        projection = session.load_projection()
        decision = decide_compaction(projection, config, window_tokens)
    except Exception:
        logger.exception(
            "%s failed to evaluate compaction; leaving transcript unchanged",
            COMPACTION_LOG_PREFIX,
        )
        return CompactionOutcome(
            status=None, decision=None, appended=None, dry_run=dry_run
        )

    status = _status_payload(decision, config, window_tokens)
    if dry_run:
        # One structured line per evaluation: "compaction would happen" as
        # well as the rejection-reason distribution for threshold tuning.
        _log_event("dry_run", decision)
        return CompactionOutcome(
            status=status,
            decision=decision,
            appended=None,
            dry_run=True,
            projection=projection,
        )

    if not decision.should_compact:
        return CompactionOutcome(
            status=status,
            decision=decision,
            appended=None,
            dry_run=False,
            projection=projection,
        )

    payload = _build_payload(
        decision.draft, session.turn_id, now or datetime.now(timezone.utc)
    )
    stored = _append_payload(session, payload)
    if stored is None:
        return CompactionOutcome(
            status=status,
            decision=decision,
            appended=None,
            dry_run=False,
            projection=projection,
        )

    _log_event(
        "committed",
        decision,
        event_seq=stored.event_seq,
        rearm_tokens=decision.draft.rearm_tokens,
    )
    # Reload once so the caller serves the committed (compacted) view.
    return CompactionOutcome(
        status=status,
        decision=decision,
        appended=stored,
        dry_run=False,
        projection=session.load_projection(),
    )


async def maybe_compact_async(
    session: AgentRolloutSession,
    config: ContextBudgetConfig,
    *,
    llm_client: Optional[Any] = None,
    llm_model: Optional[str] = None,
    window_tokens: Optional[int] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> CompactionOutcome:
    """Async coordinator entry (§3.1): summary first, prune fallback.

    Strategy selection (§3.2): when the summary trigger chain passes, the
    region is summarized and committed (``strategy="summary"``); any summary
    failure falls back to the cheap prune kernel, and a prune no-op stays a
    no-op. ``dry_run`` never calls the LLM and never writes events (§3.9).

    Threading: the event-log reads/writes hop to the worker thread; the LLM
    call stays on the event loop (it is async natively). Never raises for
    recoverable conditions.
    """
    if not config.context_compaction_enabled:
        return CompactionOutcome(
            status=None, decision=None, appended=None, dry_run=dry_run
        )

    try:
        projection = await run_in_threadpool(session.load_projection)
    except Exception:
        logger.exception(
            "%s failed to load the projection; leaving transcript unchanged",
            COMPACTION_LOG_PREFIX,
        )
        return CompactionOutcome(
            status=None, decision=None, appended=None, dry_run=dry_run
        )

    try:
        prune_decision = decide_compaction(projection, config, window_tokens)
        summary_decision = decide_summary(projection, config, window_tokens)
    except Exception:
        logger.exception(
            "%s failed to evaluate compaction; leaving transcript unchanged",
            COMPACTION_LOG_PREFIX,
        )
        return CompactionOutcome(
            status=None, decision=None, appended=None, dry_run=dry_run
        )

    status = _status_payload(prune_decision, config, window_tokens)

    if dry_run:
        # §3.9: P1 trials still ran above; the summary path only records what
        # would happen — no LLM call, no event, no projection change.
        if summary_decision.should_summarize:
            _log_summary_dry_run(summary_decision, config)
        else:
            _log_event("dry_run", prune_decision)
        return CompactionOutcome(
            status=status,
            decision=prune_decision,
            appended=None,
            dry_run=True,
            projection=projection,
        )

    # Strategy 1: summary (needs a client and a model; the cooldown from
    # earlier failures suppresses retries without affecting pruning).
    draft = None
    if (
        summary_decision.should_summarize
        and llm_client is not None
        and llm_model
        and not _summary_failures.in_cooldown(session.conv_id)
    ):
        region = summary_decision.region
        try:
            summary_text, _llm_calls = await summarize_region(
                projection.messages[: region.end],
                llm_client,
                llm_model,
                config,
                conv_id=session.conv_id,
            )
            if not summary_text:
                raise ValueError("empty summary")
            summary_content = build_summary_message_content(
                summary_text, region.end - region.start
            )
            draft = assemble_summary_draft(
                projection, region, summary_content, config, window_tokens
            )
            if draft is None:
                raise ValueError("summary replacement brings no reduction")
        except Exception as exc:
            _summary_failures.record_failure(session.conv_id, config)
            logger.warning(
                "%s summary failed; falling back to prune: %s",
                COMPACTION_LOG_PREFIX,
                str(exc)[:200],
            )
            draft = None

    if draft is not None:
        payload = _build_payload(
            draft,
            session.turn_id,
            now or datetime.now(timezone.utc),
            reason="threshold",
            strategy="summary",
        )
        stored = await run_in_threadpool(_append_payload, session, payload)
        if stored is None:
            return CompactionOutcome(
                status=status,
                decision=prune_decision,
                appended=None,
                dry_run=False,
                projection=projection,
            )
        _summary_failures.record_success(session.conv_id)
        decision = CompactionDecision(
            gate="summary",
            est_tokens=prune_decision.est_tokens,
            watermark=prune_decision.watermark,
            rearm_gate=prune_decision.rearm_gate,
            would_reclaim=draft.accounting["reclaimed"],
            would_prune_count=0,
            would_dedupe_count=0,
            draft=draft,
        )
        _log_event(
            "committed",
            decision,
            strategy="summary",
            event_seq=stored.event_seq,
            rearm_tokens=draft.rearm_tokens,
        )
        refreshed = await run_in_threadpool(session.load_projection)
        return CompactionOutcome(
            status=status,
            decision=decision,
            appended=stored,
            dry_run=False,
            projection=refreshed,
        )

    # Strategy 2: the prune kernel (Phase 0/1 behavior, incl. pressure).
    if prune_decision.should_compact:
        payload = _build_payload(
            prune_decision.draft,
            session.turn_id,
            now or datetime.now(timezone.utc),
        )
        stored = await run_in_threadpool(_append_payload, session, payload)
        if stored is None:
            return CompactionOutcome(
                status=status,
                decision=prune_decision,
                appended=None,
                dry_run=False,
                projection=projection,
            )
        _log_event(
            "committed",
            prune_decision,
            event_seq=stored.event_seq,
            rearm_tokens=prune_decision.draft.rearm_tokens,
        )
        refreshed = await run_in_threadpool(session.load_projection)
        return CompactionOutcome(
            status=status,
            decision=prune_decision,
            appended=stored,
            dry_run=False,
            projection=refreshed,
        )

    return CompactionOutcome(
        status=status,
        decision=prune_decision,
        appended=None,
        dry_run=False,
        projection=projection,
    )
