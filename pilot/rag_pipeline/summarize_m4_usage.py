#!/usr/bin/env python3
"""Sum every M4 attempt's API ledger, separately from theoretical RAG cost."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "pilot/experiments/chunking/m4_grid_manifest_v4.json")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    output = ROOT / "pilot/experiments/chunking"
    version = manifest["grid_id"].rsplit("_", 1)[-1]
    price_config = output / f"m4_text_initial_config_{version}.json"
    config = json.loads(price_config.read_text())
    rates = config["cost_estimate"]
    names = ["judge_calibration_v1", "judge_calibration_v2"]
    names += sorted(p.parent.name for p in output.glob("m4_text_initial_v*/api_events.jsonl"))
    names += [r["run_id"] for r in manifest["runs"]]
    groups, hashes = {}, {}
    for name in names:
        path = output / name / "api_events.jsonl"
        if not path.exists():
            continue
        events = [json.loads(s) for s in path.read_text().splitlines() if s.strip()]
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        roles = {}
        for role in ("embedding", "answer", "judge"):
            new = [e for e in events if e["role"] == role and e["new_api_call"]]
            inp = sum(int(e.get("usage", {}).get("prompt_tokens", e.get("usage", {}).get("total_tokens", 0) if role == "embedding" else 0)) for e in new)
            out = sum(int(e.get("usage", {}).get("completion_tokens", 0)) for e in new)
            price = rates["embedding_input_per_million"] if role == "embedding" else rates["llm_input_per_million"]
            roles[role] = {"new_requests": len(new), "reported_input_tokens": inp, "reported_output_tokens": out,
                           "recorded_errors": sum("error" in e for e in new),
                           "requests_without_response": sum("response" not in e for e in new),
                           "requests_without_usage": sum(not e.get("usage") for e in new),
                           "recorded_price_estimate_cny": round((inp * price + out * rates["llm_output_per_million"]) / 1e6, 6)}
        groups[name] = {"by_role": roles, "attempt_ids": sorted({e["attempt_id"] for e in events}),
                        "api_cache_hits": sum(e["cache_hit"] for e in events),
                        "price_estimate_cny": round(sum(v["recorded_price_estimate_cny"] for v in roles.values()), 6)}
    totals = {}
    for role in ("embedding", "answer", "judge"):
        fields = next(iter(groups.values()))["by_role"][role].keys()
        totals[role] = {k: sum(g["by_role"][role][k] for g in groups.values()) for k in fields}
        totals[role]["recorded_price_estimate_cny"] = round(totals[role]["recorded_price_estimate_cny"], 6)
    result = {"status": "ledger_snapshot_not_provider_bill", "run_folders_present": len(groups),
              "groups": groups, "by_role_total": totals,
              "total_recorded_price_estimate_cny": round(sum(v["recorded_price_estimate_cny"] for v in totals.values()), 6),
              "ledger_sha256": hashes, "price_config": str(price_config.relative_to(ROOT)),
              "notes": ["Includes both calibration runs and all partial/restarted initial attempts; M3 historical spend excluded",
                        "Only actual new API events counted; cache replay and cached vectors not charged again in this estimate",
                        "Interrupted/failed requests without reported usage may have unknown charges; these counts remain explicit",
                        "Uses the saved price assumptions, not a fresh provider invoice or guarantee of current billed price",
                        "Judge is experiment cost; online answer cost and indexing amortization are separate",
                        "Refresh after grid completes; this snapshot may represent a partial grid"]}
    (output / f"m4_usage_summary_{version}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"folders": len(groups), "recorded_price_estimate_cny": result["total_recorded_price_estimate_cny"],
                      "missing_responses": sum(v["requests_without_response"] for v in totals.values())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
