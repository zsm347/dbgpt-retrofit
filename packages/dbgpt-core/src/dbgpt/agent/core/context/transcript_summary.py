"""Summary producer for rollout transcript compaction (Phase 2, §3.3/§3.4).

Input assembly, template rendering, the LLM call and the summary message
constructor. Everything here is pure assembly plus the one async LLM call;
trigger gating, failure policy and the event commit live in the coordinator
(``dbgpt_serve.agent.db.rollout_compaction``).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

from .budget import ContextBudgetConfig
from .transcript_compaction import _content_of, _role_of, _tool_calls_of

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
    ``SUMMARY_MARKER`` detection keeps working unchanged.
    """
    return (
        f"[Context Summary of {message_count} earlier messages]\n\n"
        f"{summary_text}\n\n"
    )


async def summarize_region(
    messages: Sequence[Any],
    llm_client: Any,
    llm_model: str,
    config: ContextBudgetConfig,
    conv_id: Optional[str] = None,
) -> Tuple[str, int]:
    """Generate the summary for one region; returns (text, llm_calls).

    Raises on failure — the coordinator owns the fallback (prune) and the
    failure cooldown.
    """
    prompt = build_summary_prompt(messages)
    text = await llm_client.create(
        messages=[{"role": "user", "content": prompt}],
        llm_model=llm_model,
        max_new_tokens=2000,
        temperature=0.3,
        stream_out=False,
        conv_id=conv_id,
    )
    return (text or "").strip(), 1
