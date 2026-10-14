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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from dbgpt.agent.core.context.budget import ContextBudgetConfig
from dbgpt.agent.core.context.transcript_compaction import (
    CompactionDecision,
    compact_threshold_tokens_for,
    context_status_state,
    decide_compaction,
    effective_window_tokens,
)
from dbgpt.agent.core.rollout import ChatCompletionHistoryProjection

from .agent_event_db import AgentRolloutSession, StoredAgentEvent

logger = logging.getLogger(__name__)

COMPACTION_LOG_PREFIX = "context_compaction"


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
    }
    fields.update(extra)
    logger.info(
        "%s %s",
        COMPACTION_LOG_PREFIX,
        json.dumps(fields, ensure_ascii=False, sort_keys=True),
    )


def _build_payload(draft: Any, turn_id: str, now: datetime) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "reason": "proactive",
        "strategy": "prune",
        "replacement_history": draft.replacement_history,
        "covers": {"through_event_seq": draft.through_event_seq},
        "accounting": draft.accounting,
        "rearm_tokens": draft.rearm_tokens,
        "created_by": {
            "turn_id": turn_id,
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


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
    try:
        stored = session.append("context_compacted", payload)
    except Exception:
        # The append is the only commit point: on any failure (e.g. a lost
        # turn lease) nothing changed and the next step retries.
        logger.warning(
            "%s append failed; leaving transcript unchanged",
            COMPACTION_LOG_PREFIX,
            exc_info=True,
        )
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
