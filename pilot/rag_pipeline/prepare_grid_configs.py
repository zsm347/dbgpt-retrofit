#!/usr/bin/env python3
"""Create explicit run configs for every distinct preflighted block set."""
import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "pilot/experiments/chunking"


def main() -> None:
    candidate_path = OUTPUT / "m4_candidates_v1.json"
    base_path = OUTPUT / "m4_text_initial_config_v1.json"
    candidates = json.loads(candidate_path.read_text())
    baseline = json.loads(base_path.read_text())
    config_root = OUTPUT / "m4_grid_configs_v1"
    config_root.mkdir(exist_ok=True)
    runs = []
    for candidate in candidates["candidates"]:
        if not candidate["valid"] or candidate["equivalent_to"]:
            continue
        config = copy.deepcopy(baseline)
        identity = candidate["candidate_id"]
        config["run_id"] = "m4_grid_v1_" + identity
        config["report_title"] = "M4 参数实验（试验性）：" + identity
        config["purpose"] = "Frozen prose chunking coarse candidate; all 24 tuning questions; human review pending"
        config["chunking"].update({"strategies": [candidate["strategy"]], "chunk_size": candidate["chunk_size"],
                                   "overlap": candidate["overlap"], "overlap_ratio": candidate["overlap_ratio"]})
        config["candidate"] = {"id": identity, "expected_block_fingerprint": candidate["block_fingerprint"],
                               "declared_aliases": [c["candidate_id"] for c in candidates["candidates"] if c["equivalent_to"] == identity]}
        config["execution"]["grid_policy"] = "Run this one distinct frozen block set, retaining declared equivalent identities"
        config["execution"]["max_new_requests"] = 1500
        config["cost_estimate"]["estimate_range_cny"] = [1, 4]
        path = config_root / f"{identity}.json"
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        runs.append({"candidate_id": identity, "run_id": config["run_id"], "config_path": str(path.relative_to(ROOT)),
                     "config_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                     "declared_aliases": config["candidate"]["declared_aliases"]})
    manifest = {"grid_id": "m4_grid_2026-10-09_v1", "status": "configs_prepared_model_runs_not_started",
                "qa_sha256": candidates["qa_sha256"], "declared_candidate_count": candidates["declared_candidates"],
                "distinct_runs": len(runs), "candidate_file_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                "initial_config_sha256": hashlib.sha256(base_path.read_bytes()).hexdigest(),
                "review_status": "agent_source_reviewed_pending_human_review", "runs": runs}
    (OUTPUT / "m4_grid_manifest_v1.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"declared": manifest["declared_candidate_count"], "distinct_run_configs": len(runs), "status": manifest["status"]}))


if __name__ == "__main__":
    main()
