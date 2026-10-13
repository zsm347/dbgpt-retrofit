"""Run wording variants against the existing real-model observation runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import observe_real_model as runner


VARIANT_FIXTURE = Path(__file__).parent / "fixtures" / "memory_variants.json"


def main() -> None:
    runner.CASES = json.loads(VARIANT_FIXTURE.read_text())
    runner.RESULT["fixture"] = str(VARIANT_FIXTURE.relative_to(runner.REPO))
    asyncio.run(runner.main())


if __name__ == "__main__":
    main()
