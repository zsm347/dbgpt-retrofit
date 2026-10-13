"""Score LongMemEval predictions with a DashScope judge.

This follows the task-specific correctness criteria in LongMemEval's QA
evaluator, but uses a configurable DashScope judge instead of the benchmark's
default GPT-4o evaluator. Results must therefore be labeled with the judge model
and are not directly comparable to the paper's GPT-4o scores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI


QUESTION_TYPES = (
    "single-session-user",
    "single-session-preference",
    "single-session-assistant",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_dashscope_profile(path: Path) -> tuple[str, str]:
    """Read the key and compatible endpoint without printing either value."""

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    base_url = os.environ.get("DASHSCOPE_BASE_URL")
    if api_key and base_url:
        return api_key, base_url

    if not path.is_file():
        raise FileNotFoundError(
            "Set DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL, or provide an existing "
            f"DB-GPT profile with DashScope credentials: {path}"
        )

    config = tomllib.loads(path.read_text(encoding="utf-8"))
    llm_entries = config.get("models", {}).get("llms", [])
    for entry in llm_entries:
        key = entry.get("api_key")
        endpoint = entry.get("api_base")
        provider = str(entry.get("provider", "")).lower()
        if key and endpoint and ("tongyi" in provider or "dashscope" in endpoint):
            return str(key), str(endpoint)
    raise ValueError(f"No DashScope LLM credentials were found in profile: {path}")


def _judge_prompt(item: dict[str, Any], hypothesis: str) -> str:
    qid = item["question_id"]
    question_type = item["question_type"]
    question = item["question"]
    reference = item["answer"]
    is_abstention = "_abs" in qid

    if is_abstention:
        rubric = (
            "This is an unanswerable-question case. Mark YES only if the response "
            "recognizes that the requested information is missing or insufficient. "
            "It may state partial facts that are present, as long as it does not "
            "pretend the missing answer is known."
        )
    elif question_type == "single-session-preference":
        rubric = (
            "The reference is a rubric for the desired personalized response. Mark "
            "YES when the response correctly recalls and uses the user's personal "
            "information; it does not need to mention every rubric detail."
        )
    elif question_type == "temporal-reasoning":
        rubric = (
            "Judge whether the response gives the correct time-based answer. For a "
            "duration expressed in days, allow an off-by-one-day difference."
        )
    elif question_type == "knowledge-update":
        rubric = (
            "Judge against the required updated fact. Including an earlier fact as "
            "context is acceptable if the response still gives the requested current "
            "or updated answer."
        )
    else:
        rubric = (
            "Mark YES if the response contains the required answer or an equivalent "
            "answer. Mark NO if it omits an essential part or gives a conflicting answer."
        )

    return (
        "You are grading one answer for a long-term conversational-memory benchmark. "
        "Apply the rubric carefully and return only YES or NO.\n\n"
        f"Rubric: {rubric}\n\n"
        f"Question: {question}\n\n"
        f"Reference answer or rubric: {reference}\n\n"
        f"System response: {hypothesis}\n\n"
        "Is the system response correct?"
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[bool]] = defaultdict(list)
    abstention: list[bool] = []
    all_labels: list[bool] = []
    for result in results:
        label = bool(result["correct"])
        all_labels.append(label)
        by_type[result["question_type"]].append(label)
        if result["is_abstention"]:
            abstention.append(label)

    type_metrics = {
        kind: {
            "accuracy": round(sum(labels) / len(labels), 4) if labels else None,
            "correct": sum(labels),
            "count": len(labels),
        }
        for kind, labels in sorted(by_type.items())
    }
    active_type_scores = [m["accuracy"] for m in type_metrics.values() if m["accuracy"] is not None]
    return {
        "judge_type": "dashscope_custom",
        "overall_accuracy": round(sum(all_labels) / len(all_labels), 4) if all_labels else None,
        "overall_correct": sum(all_labels),
        "overall_count": len(all_labels),
        "task_averaged_accuracy_over_present_types": (
            round(statistics.mean(active_type_scores), 4) if active_type_scores else None
        ),
        "by_question_type": type_metrics,
        "abstention_accuracy": round(sum(abstention) / len(abstention), 4) if abstention else None,
        "abstention_correct": sum(abstention),
        "abstention_count": len(abstention),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", nargs="+", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path.home() / ".dbgpt" / "configs" / "qwen.toml",
    )
    parser.add_argument("--judge-model", default="qwen3.8-max")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    predictions: list[dict[str, Any]] = []
    for prediction_file in args.predictions:
        predictions.extend(_read_jsonl(prediction_file))
    ids = [row["question_id"] for row in predictions]
    if len(ids) != len(set(ids)):
        raise ValueError("Prediction inputs contain duplicate question_id values")

    raw_dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    reference_by_id = {row["question_id"]: row for row in raw_dataset}
    missing = [qid for qid in ids if qid not in reference_by_id]
    if missing:
        raise ValueError(f"Prediction IDs are missing from reference data: {missing}")
    invalid = [row["question_id"] for row in predictions if not row.get("hypothesis", "").strip()]
    if invalid:
        raise ValueError(f"Predictions have empty hypotheses: {invalid}")

    api_key, base_url = _load_dashscope_profile(args.profile)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=args.timeout, max_retries=2)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.output_dir / "judge_results.jsonl"
    results: list[dict[str, Any]] = []
    with results_path.open("w", encoding="utf-8") as output:
        for index, prediction in enumerate(predictions, start=1):
            item = reference_by_id[prediction["question_id"]]
            prompt = _judge_prompt(item, prediction["hypothesis"])
            completion = client.chat.completions.create(
                model=args.judge_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=32,
            )
            raw_judgment = (completion.choices[0].message.content or "").strip()
            normalized = raw_judgment.upper().strip(" .!\n\t")
            if normalized.startswith("YES"):
                correct = True
            elif normalized.startswith("NO"):
                correct = False
            else:
                raise ValueError(
                    f"Judge returned an unrecognized label for {prediction['question_id']}: "
                    f"{raw_judgment!r}"
                )

            result = {
                "question_id": prediction["question_id"],
                "question_type": item["question_type"],
                "is_abstention": "_abs" in prediction["question_id"],
                "correct": correct,
                "judge_model": args.judge_model,
                "judge_response": raw_judgment,
                "hypothesis": prediction["hypothesis"],
                "reference_answer": item["answer"],
            }
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            results.append(result)
            print(f"[{index}/{len(predictions)}] {prediction['question_id']}: {'YES' if correct else 'NO'}", flush=True)

    metrics = _compute_metrics(results)
    metrics["judge_model"] = args.judge_model
    metrics["scored_at"] = datetime.now(timezone.utc).isoformat()
    (args.output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    source_hashes = {str(path): _sha256(path) for path in args.predictions}
    manifest = {
        "run_id": args.output_dir.name,
        "status": "completed",
        "judge_type": "custom_dashscope",
        "judge_model": args.judge_model,
        "provider": "Alibaba Cloud Model Studio / DashScope OpenAI-compatible chat API",
        "base_url": base_url,
        "profile_path": str(args.profile),
        "api_key_logged": False,
        "prompt_protocol": "LongMemEval task-specific correctness criteria, adapted for DashScope; not the official GPT-4o judge prompt run",
        "temperature": 0,
        "max_tokens": 32,
        "dataset_path": str(args.dataset.resolve()),
        "dataset_sha256": _sha256(args.dataset),
        "prediction_files": source_hashes,
        "scored_count": len(results),
        "metrics_path": str((args.output_dir / "metrics.json").resolve()),
        "results_path": str(results_path.resolve()),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
