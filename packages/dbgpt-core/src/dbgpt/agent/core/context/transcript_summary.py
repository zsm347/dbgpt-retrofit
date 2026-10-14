"""Summary producer for rollout transcript compaction (Phase 2, §3.3/§3.4).

Input assembly, template rendering, the LLM call and the summary message
constructor. Everything here is pure assembly plus the one async LLM call;
trigger gating, failure policy and the event commit live in the coordinator
(``dbgpt_serve.agent.db.rollout_compaction``).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

from dbgpt.util.error_types import LLMChatError

from .budget import ContextBudgetConfig
from .transcript_compaction import (
    _content_of,
    _role_of,
    _tool_calls_of,
    detect_turns,
)

# Six-section template, migrated verbatim from the legacy L3 prompt
# (compact.py) — see §3.7 "sections6".
SECTIONS6_TEMPLATE = """\
You are a context summarizer for a ReAct agent. Condense the conversation history \
into a structured summary. Be precise — preserve exact names, paths, values, and \
variable names.

## Conversation to summarize
{conversation_text}

## Output format (use exactly these headings)
1. Original Task: <one-line description>
2. Completed Steps: <one line per step, action + key result>
3. Current State: <what the agent currently knows>
4. Key Data: <important values, paths, variable names — must be exact>
5. Errors Encountered: <failures and how they were resolved, or "None">
6. Next Steps: <remaining work, or "None" if task is complete>
"""

# Codex-style four-section handoff summary ("for another LLM about to take
# over"), see §3.7 "handoff".
HANDOFF_TEMPLATE = """\
You are writing a handoff summary for another LLM that will resume this task. \
Be precise — preserve exact names, paths, values, and variable names.

## Conversation to summarize
{conversation_text}

## Output format (use exactly these headings)
1. Progress: <what has been done, and the key decisions taken>
2. Context: <constraints, environment details, and user preferences that matter>
3. What Remains: <the remaining work, or "None" if the task is complete>
4. Critical Data: <exact values, paths, variable names, ids — must be precise>
"""

# Combined third variant (§3.7, default): six-section skeleton plus a
# "User Constraints & Preferences" slot, handoff framing and an explicit
# do-not-redo closing.
HYBRID_TEMPLATE = """\
You are writing a handoff summary for another LLM that will resume this task. \
Condense the conversation history into a structured summary. Be precise — \
preserve exact names, paths, values, and variable names.

## Conversation to summarize
{conversation_text}

## Output format (use exactly these headings)
1. Original Task: <one-line description>
2. Completed Steps: <one line per step, action + key result>
3. Current State: <what the agent currently knows>
4. Key Data: <important values, paths, variable names — must be exact>
5. User Constraints & Preferences: <explicit user constraints, \
preferences and corrections — or "None">
6. Errors Encountered: <failures and how they were resolved, or "None">
7. Next Steps: <remaining work, or "None" if task is complete>

The work already completed must not be redone; continue only from Next Steps.
"""

SUMMARY_TEMPLATES = {
    "sections6": SECTIONS6_TEMPLATE,
    "handoff": HANDOFF_TEMPLATE,
    "hybrid": HYBRID_TEMPLATE,
}


def summary_template_name(config: ContextBudgetConfig) -> str:
    """Resolve the configured template name; unknown values fall back."""
    name = str(getattr(config, "summary_template", None) or "hybrid").lower()
    return name if name in SUMMARY_TEMPLATES else "hybrid"


def summary_template_for(config: ContextBudgetConfig) -> str:
    """Resolve the configured summary template text."""
    return SUMMARY_TEMPLATES[summary_template_name(config)]


# Hermes' _SUMMARY_END_MARKER: tells the model to answer the live message,
# not the summary above (§3.4).
SUMMARY_END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)

# Best-effort context-overflow classification by text (§3.3): the repo has no
# dedicated exception type, so these substrings are matched against both the
# wrapper error and its original exception.
_CONTEXT_OVERFLOW_PATTERNS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "exceeds the maximum",
    "413",
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """True when an LLM error looks like a context-window overflow (§3.3)."""
    text = str(exc).lower()
    if isinstance(exc, LLMChatError) and exc.original_exception is not None:
        text += " " + str(exc.original_exception).lower()
    return any(pattern in text for pattern in _CONTEXT_OVERFLOW_PATTERNS)


def serialize_region(messages: Sequence[Any]) -> str:
    """Serialize the summary region (§3.3): full content, never truncated.

    ``[{role}]: {content}`` per message, plus ``[tool_calls] {name}({arguments})``
    for assistant messages carrying native tool calls (arguments included in
    full — bounding is deliberately not applied, trim-retry is the fallback).
    """
    lines: list[str] = []
    for message in messages:
        lines.append(f"[{_role_of(message)}]: {_content_of(message)}")
        for tool_call in _tool_calls_of(message) or []:
            if not isinstance(tool_call, Mapping):
                continue
            name = ""
            arguments = ""
            function = tool_call.get("function")
            if isinstance(function, Mapping):
                raw_name = function.get("name")
                raw_args = function.get("arguments")
                name = raw_name if isinstance(raw_name, str) else ""
                arguments = raw_args if isinstance(raw_args, str) else ""
            lines.append(f"[tool_calls] {name}({arguments})")
    return "\n".join(lines)


def build_summary_prompt(
    messages: Sequence[Any], template: str = SECTIONS6_TEMPLATE
) -> str:
    """Render the summary prompt for a region (single user message, §3.3)."""
    return template.format(conversation_text=serialize_region(messages))


def build_summary_message_content(summary_text: str, message_count: int) -> str:
    """Construct the summary message content (§3.4).

    The ``[Context Summary of ...]`` prefix matches the legacy L3 summary so
    ``SUMMARY_MARKER`` detection keeps working unchanged; the end marker
    tells the model to respond to the live message below the summary.
    """
    return (
        f"[Context Summary of {message_count} earlier messages]\n\n"
        f"{summary_text}\n\n"
        f"{SUMMARY_END_MARKER}"
    )


async def summarize_region(
    messages: Sequence[Any],
    llm_client: Any,
    llm_model: str,
    config: ContextBudgetConfig,
    conv_id: Optional[str] = None,
) -> Tuple[str, int]:
    """Generate the summary for one region; returns (text, llm_calls).

    The call goes straight through ``llm_client.create`` (not
    ``generate_text``, which swallows errors into an empty string) with an
    explicit uncapped ``max_new_tokens=None`` (F7). On a context-window
    overflow the oldest turn is dropped from the input and the call is
    retried; the loop terminates once a single turn remains. The
    ``summary_trim_max_attempts`` budget only bounds retries that make no
    progress, and a successful trim resets it (F4).

    Raises on failure — the coordinator owns the fallback (prune) and the
    failure cooldown.
    """
    turns = detect_turns(messages)
    if not turns:
        raise ValueError("nothing to summarize")
    working = list(turns)
    max_no_progress = max(1, int(config.summary_trim_max_attempts))
    no_progress = 0
    llm_calls = 0

    template = summary_template_for(config)
    while True:
        sliced = [messages[index] for turn in working for index in turn.indices]
        prompt = build_summary_prompt(sliced, template)
        llm_calls += 1
        try:
            text = await llm_client.create(
                messages=[{"role": "user", "content": prompt}],
                llm_model=llm_model,
                max_new_tokens=None,  # F7: explicit None = no output cap
                temperature=0.3,
                stream_out=False,
                conv_id=conv_id,
            )
        except Exception as exc:
            if not is_context_overflow_error(exc):
                # Non-overflow failure: no trimming, straight to the failure
                # path (the coordinator counts it and cools down).
                raise
            if len(working) <= 1:
                # Terminator: a single turn cannot be trimmed any further.
                raise
            before = len(working)
            working = working[1:]
            if len(working) < before:
                no_progress = 0  # progress resets the retry budget (F4)
            else:
                no_progress += 1
                if no_progress >= max_no_progress:
                    raise
            continue
        return (text or "").strip(), llm_calls
