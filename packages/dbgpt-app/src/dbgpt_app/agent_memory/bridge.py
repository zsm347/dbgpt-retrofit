"""A small optional adapter from a DB-GPT turn to the memory prototype.

This module deliberately owns no memory schema or extraction policy.  It loads
the independently versioned ``examples/agent_memory_v1`` package only when the
caller enables the bridge.  The prototype remains the implementation of
search, LangMem consolidation, validation, and Mem0 persistence.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_UNSET = object()
_bridge_cache: object = _UNSET


def _enabled() -> bool:
    return os.getenv("DBGPT_AGENT_MEMORY_ENABLED", "").strip().lower() in _TRUE_VALUES


def _prototype_root() -> Path:
    configured = os.getenv("DBGPT_AGENT_MEMORY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    # bridge.py -> agent_memory -> dbgpt_app -> src -> dbgpt-app -> packages
    # -> repository root
    return Path(__file__).resolve().parents[5] / "examples" / "agent_memory_v1"


def _load_prototype_dependencies(root: Path) -> None:
    """Expose the prototype's independent virtualenv to this process.

    DB-GPT deliberately does not depend on Mem0/LangMem at the root project
    level.  The optional bridge therefore has to make the pinned environment
    in ``examples/agent_memory_v1/.venv`` importable when the feature is
    enabled.  Missing environments are left to the caller's normal graceful
    fallback path.
    """

    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        root / ".venv" / "lib" / version / "site-packages",
        root / ".venv" / "Lib" / "site-packages",  # Windows layout
    ]
    for site_packages in candidates:
        if site_packages.is_dir() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))
            return


def _scope_id(value: str) -> str:
    """Make an application identity safe for the prototype's scope contract."""

    return quote(value.strip(), safe="-_.:@")


class AgentMemoryBridge:
    """Lazy adapter for one process-wide ``memory_v1.MemoryService`` instance."""

    def __init__(self, memory_module: Any, service: Any):
        self._memory = memory_module
        self._service = service

    @classmethod
    def from_environment(cls) -> Optional["AgentMemoryBridge"]:
        """Load the optional prototype, returning ``None`` on a safe fallback."""

        if not _enabled():
            return None

        root = _prototype_root()
        if root.is_dir() and str(root) not in sys.path:
            sys.path.insert(0, str(root))
        _load_prototype_dependencies(root)
        # Mem0's optional telemetry client is not part of the memory contract.
        # Disable it before importing Mem0 so a DB-GPT process that already
        # loaded another PostHog version cannot break the optional bridge.
        os.environ.setdefault("MEM0_TELEMETRY", "false")
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
        try:
            memory_module = importlib.import_module("memory_v1")
            service = memory_module.MemoryService.from_config()
        except Exception:
            logger.warning(
                "DB-GPT long-term memory is enabled but the optional "
                "agent_memory_v1 environment could not be loaded; continuing "
                "without memory",
                exc_info=True,
            )
            return None
        logger.info("DB-GPT long-term memory bridge enabled from %s", root)
        return cls(memory_module, service)

    def scope_for(
        self,
        *,
        user_name: Optional[str],
        app_code: Optional[str],
        sys_code: Optional[str],
        conv_id: str,
    ) -> Any:
        """Map a DB-GPT conversation to a stable user or isolated project scope."""

        user = (user_name or "").strip()
        if user:
            return self._memory.MemoryScope(scope_type="user", scope_id=_scope_id(user))

        # Anonymous conversations must not share memories merely because they
        # use the same application.  A named user can share memories across
        # conversations; an anonymous caller gets a conversation-local scope.
        project = (app_code or sys_code or "react_agent").strip() or "react_agent"
        return self._memory.MemoryScope(
            scope_type="project",
            scope_id=_scope_id(f"{project}:{conv_id}"),
        )

    def source_message(
        self,
        *,
        message_id: str,
        role: str,
        content: str,
        observed_at: Optional[datetime],
    ) -> Any:
        """Create a prototype source object without exposing its class upstream."""

        return self._memory.SourceMessage(
            id=message_id,
            role=role,
            content=content,
            observed_at=observed_at,
        )

    @staticmethod
    def _aware(value: Optional[datetime]) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    async def search_context(
        self,
        *,
        scope: Any,
        question: str,
        reference_time: datetime,
        top_k: int = 10,
    ) -> str:
        """Retrieve structured candidates and render them for the answer model."""

        records = await self._service.search(scope, question, top_k=top_k)
        if not records:
            return ""
        context = self._memory.build_answer_context(
            records,
            reference_time=self._aware(reference_time),
        )
        return (
            "\n\n## Long-term memory candidates\n"
            "The following are retrieved memory candidates. Use them only when "
            "they support the user's question; preserve source and time limits.\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )

    async def record_turn(
        self,
        *,
        scope: Any,
        conv_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        observed_at: datetime,
        recent_messages: Iterable[Any] = (),
    ) -> Any:
        """Persist one completed user/assistant turn; tool messages are excluded."""

        observed_at = self._aware(observed_at)
        messages = [
            self.source_message(
                message_id=f"{conv_id}:{turn_id}:user",
                role="user",
                content=user_text,
                observed_at=observed_at,
            ),
            self.source_message(
                message_id=f"{conv_id}:{turn_id}:assistant",
                role="assistant",
                content=assistant_text,
                observed_at=datetime.now(timezone.utc),
            ),
        ]
        return await self._service.add(
            scope,
            messages,
            list(recent_messages),
            batch_id=turn_id,
            include_tool_in_query=False,
        )


def get_agent_memory_bridge() -> Optional[AgentMemoryBridge]:
    """Return the cached optional bridge without affecting the default path."""

    global _bridge_cache
    if _bridge_cache is _UNSET:
        _bridge_cache = AgentMemoryBridge.from_environment()
    return _bridge_cache if isinstance(_bridge_cache, AgentMemoryBridge) else None


def _reset_agent_memory_bridge_for_tests() -> None:
    """Reset the process cache for focused adapter tests."""

    global _bridge_cache
    _bridge_cache = _UNSET
