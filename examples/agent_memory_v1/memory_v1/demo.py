"""Explicit opt-in demo for the independent write path.

Run with ``AGENT_MEMORY_DEMO_WRITE=1`` only when a real DashScope-compatible
model and an intentionally chosen local data directory are configured.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from .schema import MemoryScope, SourceMessage
from .service import MemoryService
from .store import MemoryStoreConfig


async def run() -> None:
    config = MemoryStoreConfig.from_env()
    if config.api_key is None or not config.api_key.get_secret_value().strip():
        print(
            "No DASHSCOPE_API_KEY found. The demo only validates configuration "
            "and will not initialize Mem0 or write data."
        )
        print(
            f"Configured models: chat={config.chat_model}, "
            f"embedding={config.embedding_model} ({config.embedding_dims} dims); "
            f"collection={config.collection_name}; data_dir={config.data_dir}"
        )
        return

    if os.getenv("AGENT_MEMORY_DEMO_WRITE") != "1":
        print(
            "A model key is configured, but the demo is write-disabled by default. "
            "Set AGENT_MEMORY_DEMO_WRITE=1 after reviewing the data directory to run it."
        )
        return

    observed_at = datetime.now(timezone.utc)
    service = MemoryService.from_config(config)
    summary = await service.add(
        scope=MemoryScope(scope_type="user", scope_id="demo-user"),
        new_messages=[
            SourceMessage(
                id="demo-message-user-1",
                role="user",
                content="我偏好先给结论，再给简短依据。",
                observed_at=observed_at,
            ),
            SourceMessage(
                id="demo-message-assistant-1",
                role="assistant",
                content="我会在后续回答中先给结论，再补充依据。",
                observed_at=observed_at,
            ),
        ],
        batch_id="demo-batch-1",
    )
    print(summary.model_dump_json(indent=2, exclude_none=True))


if __name__ == "__main__":
    asyncio.run(run())
