#!/usr/bin/env python3
"""Convert project QA/run records to a Ragas-compatible JSONL shape.

This module intentionally does not import or install Ragas.  The project can
validate and freeze its data schema before selecting a Ragas version and
model provider.  The resulting JSONL uses the canonical SingleTurnSample
field names and retains project metadata under ``metadata``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "pilot/data/eval/qa_provisional.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "pilot/data/eval/ragas_input_provisional.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def to_ragas_record(
    qa: dict[str, Any],
    *,
    retrieved_contexts: list[str] | None = None,
    response: str | None = None,
    retrieved_context_ids: list[str] | None = None,
) -> dict[str, Any]:
    required = ("qa_id", "question", "reference_answer", "reference_contexts")
    missing = [field for field in required if field not in qa]
    if missing:
        raise ValueError(f"{qa.get('qa_id', '<unknown>')}: missing fields {missing}")
    if not qa["question"].strip() or not qa["reference_answer"].strip():
        raise ValueError(f"{qa['qa_id']}: question and reference_answer must be non-empty")
    if not qa["reference_contexts"]:
        raise ValueError(f"{qa['qa_id']}: reference_contexts must be non-empty")

    sample: dict[str, Any] = {
        "user_input": qa["question"],
        "reference": qa["reference_answer"],
        "reference_contexts": qa["reference_contexts"],
        "metadata": {
            "qa_id": qa["qa_id"],
            "qa_version": qa.get("qa_version"),
            "corpus_version": qa.get("corpus_version"),
            "doc_id": qa.get("doc_id"),
            "doc_path": qa.get("doc_path"),
            "content_sha256": qa.get("content_sha256"),
            "source_spans": qa.get("source_spans", []),
            "reference_context_spans": qa.get("reference_context_spans", []),
            "reference_context_ids": qa.get("reference_context_ids", []),
            "question_type": qa.get("question_type"),
            "difficulty": qa.get("difficulty"),
            "table_dependent": qa.get("table_dependent", False),
            "image_dependent": qa.get("image_dependent", False),
            "split": qa.get("split"),
            "review_status": qa.get("review_status"),
            "generation_method": qa.get("generation_method"),
            "source_scope": qa.get("source_scope"),
            "version_scope": qa.get("version_scope"),
            "question_language": qa.get("question_language"),
            "fact_id": qa.get("fact_id"),
            "topic_family": qa.get("topic_family"),
            "pair_group_id": qa.get("pair_group_id"),
            "question_variant": qa.get("question_variant"),
        },
    }
    if retrieved_contexts is not None:
        sample["retrieved_contexts"] = retrieved_contexts
    if retrieved_context_ids is not None:
        sample["retrieved_context_ids"] = retrieved_context_ids
    if response is not None:
        sample["response"] = response
    return sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    qa_records = read_jsonl(args.input)
    output = [to_ragas_record(record) for record in qa_records]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in output:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"records": len(output), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
