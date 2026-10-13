#!/usr/bin/env python3
"""Run frozen candidate configs sequentially, with durable progress and caches.

Requires the complete initial comparison and successful controlled judge checks.
No approval interaction; incomplete human review remains explicit in all outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "pilot/experiments/chunking/m4_grid_manifest_v1.json")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    initial = ROOT / manifest.get("initial_summary_path", "pilot/experiments/chunking/m4_text_initial_v1/summary.json")
    if not initial.exists():
        raise RuntimeError("Run the full 24-question initial comparison before starting the grid")
    initial_summary = json.loads(initial.read_text())
    if initial_summary["error_count"]:
        raise RuntimeError("Resolve initial-comparison failures before starting the grid")
    if sum(g["questions"] for g in initial_summary["groups"]) != 96:
        raise RuntimeError("The initial comparison must contain all 24 questions in both strategies and views")
    progress_path = args.manifest.with_name(manifest.get("progress_file", "m4_grid_progress_v1.json"))
    progress = {"grid_id": manifest["grid_id"], "status": "running", "completed": [],
                "review_status": manifest["review_status"], "declared_candidate_count": manifest["declared_candidate_count"]}
    save(progress_path, progress)
    for run in manifest["runs"]:
        config_path = ROOT / run["config_path"]
        if hashlib.sha256(config_path.read_bytes()).hexdigest() != run["config_file_sha256"]:
            raise RuntimeError("Frozen candidate config changed; create a new manifest version")
        config = json.loads(config_path.read_text())
        summary_path = ROOT / config["execution"]["output_root"] / run["run_id"] / "summary.json"
        if not summary_path.exists() or json.loads(summary_path.read_text())["error_count"]:
            result = subprocess.run([sys.executable, str(ROOT / "pilot/rag_pipeline/run_model_eval.py"),
                                     "--config", str(config_path)], cwd=ROOT, check=False)
            if result.returncode or not summary_path.exists() or json.loads(summary_path.read_text())["error_count"]:
                progress.update({"status": "needs_failure_recovery", "failed_candidate": run["candidate_id"]})
                save(progress_path, progress)
                raise RuntimeError("Candidate failed; inspect its saved error/checkpoint and rerun to recover successful cached calls")
        progress["completed"].append({**run, "summary_path": str(summary_path.relative_to(ROOT))})
        save(progress_path, progress)
    progress["status"] = "runs_completed_pending_paired_analysis_and_human_review"
    save(progress_path, progress)
    print(json.dumps({"grid_id": manifest["grid_id"], "distinct_completed": len(progress["completed"]), "status": progress["status"]}))


if __name__ == "__main__":
    main()
