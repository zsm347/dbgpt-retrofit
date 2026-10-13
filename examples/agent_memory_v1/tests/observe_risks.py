"""Run focused source, time, and provenance observations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import observe_real_model as runner


RISK_FIXTURE = Path(__file__).parent / "fixtures" / "memory_risks.json"


def main() -> None:
    runner.CASES = json.loads(RISK_FIXTURE.read_text())
    runner.RESULT["fixture"] = str(RISK_FIXTURE.relative_to(runner.REPO))
    asyncio.run(runner.main())


if __name__ == "__main__":
    main()
