"""Optional long-term memory bridge for the DB-GPT agent path.

The bridge is disabled unless ``DBGPT_AGENT_MEMORY_ENABLED=1``.  Keeping the
feature optional lets the main DB-GPT installation run without the independent
Mem0/LangMem prototype dependencies.
"""

from .bridge import AgentMemoryBridge, get_agent_memory_bridge

__all__ = ["AgentMemoryBridge", "get_agent_memory_bridge"]
