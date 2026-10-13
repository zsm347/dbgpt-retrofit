#!/usr/bin/env python3
"""Resolve the first-round QA seed into a traceable evaluation dataset.

The seed is deliberately kept separate from the resolved dataset.  A seed
record names source line ranges; this script turns those ranges into exact
character offsets and evidence text against the frozen corpus version.  It
does not call an LLM and it does not claim that the records have received
human content review.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "pilot/data/eval/corpus_manifest.json"
DEFAULT_SEED = PROJECT_ROOT / "pilot/data/eval/qa_seed.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "pilot/data/eval/qa_provisional.jsonl"
DEFAULT_SPLITS = PROJECT_ROOT / "pilot/data/eval/qa_splits.json"
DEFAULT_REPORT = PROJECT_ROOT / "pilot/data/eval/qa_quality_report.md"
DEFAULT_REVIEW_LOG = PROJECT_ROOT / "pilot/data/eval/qa_review_log.md"


# Hold out complete documents so that the same source fact does not appear in
# both tuning and retained evaluation questions.  Related documents are kept
# together where practical (the two SMMF documents are one topic family).
HOLDOUT_DOC_PATHS = {
    "modules/smmf.md",
    "application/advanced_tutorial/smmf.md",
    "agents/introduction/tools.md",
    "application/graph_rag.md",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - clear CLI error
            raise ValueError(f"Invalid JSON on {path}:{line_no}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Expected an object on {path}:{line_no}")
        records.append(value)
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def line_offsets(text: str) -> tuple[list[str], list[int]]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    return lines, offsets


def span_from_lines(
    text: str,
    lines: list[str],
    offsets: list[int],
    line_start: int,
    line_end: int,
) -> dict[str, Any]:
    if line_start < 1 or line_end < line_start or line_end > len(lines):
        raise ValueError(
            f"Invalid source line range {line_start}-{line_end}; "
            f"document has {len(lines)} lines"
        )
    start = offsets[line_start - 1]
    end_with_newline = offsets[line_end - 1] + len(lines[line_end - 1])
    # Exclude only the final line ending. Internal line endings are part of
    # the source evidence and therefore remain in the span text.
    end = end_with_newline
    while end > start and text[end - 1] in "\r\n":
        end -= 1
    value = text[start:end]
    if not value.strip():
        raise ValueError(f"Source line range {line_start}-{line_end} is blank")
    return {
        "line_start": line_start,
        "line_end": line_end,
        "start_char": start,
        "end_char": end,
        "text": value,
    }


def nearest_heading(text: str, start_char: int) -> str | None:
    # Include the line containing start_char.  This matters when an evidence
    # range starts on a heading (for example, a Markdown table section).
    line_end = text.find("\n", start_char)
    if line_end == -1:
        line_end = len(text)
    prefix = text[:line_end]
    heading: str | None = None
    for line in prefix.splitlines():
        if line.startswith("#"):
            heading = line.strip()
    return heading


def resolve_record(
    seed: dict[str, Any],
    manifest_doc: dict[str, Any],
    corpus_version: str,
) -> dict[str, Any]:
    path = Path(manifest_doc["canonical_path"])
    text = path.read_text(encoding="utf-8")
    actual_sha = sha256_file(path)
    expected_sha = manifest_doc["content_sha256"]
    if actual_sha != expected_sha:
        raise ValueError(
            f"SHA-256 mismatch for {seed['doc_path']}: "
            f"manifest={expected_sha}, actual={actual_sha}"
        )
    lines, offsets = line_offsets(text)
    def resolve_ranges(ranges: list[list[int]]) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line_range in ranges:
            if not isinstance(line_range, list) or len(line_range) != 2:
                raise ValueError(f"{seed['qa_id']}: invalid source range {line_range!r}")
            span = span_from_lines(text, lines, offsets, int(line_range[0]), int(line_range[1]))
            # Identity names the source version and actual position, rather
            # than an ordinal that collides across questions in one document.
            span["evidence_id"] = (
                f"{manifest_doc['doc_id']}:{actual_sha}:"
                f"{span['start_char']}:{span['end_char']}"
            )
            if span["evidence_id"] in seen:
                raise ValueError(f"{seed['qa_id']}: duplicate source range {line_range!r}")
            seen.add(span["evidence_id"])
            span["heading"] = nearest_heading(text, span["start_char"])
            spans.append(span)
        return spans

    evidence = resolve_ranges(seed["evidence_line_ranges"])

    if not evidence:
        raise ValueError(f"{seed['qa_id']}: at least one evidence span is required")

    # A table review may show the complete table as reference context while
    # marking only its header and answer-bearing rows as supporting evidence.
    # Unrelated rows must not inflate the denominator of a coverage metric.
    contexts = resolve_ranges(seed.get("context_line_ranges", seed["evidence_line_ranges"]))
    for span in evidence:
        if not any(
            context["start_char"] <= span["start_char"]
            and context["end_char"] >= span["end_char"]
            for context in contexts
        ):
            raise ValueError(f"{seed['qa_id']}: reference context omits evidence {span['evidence_id']}")

    record = {
        "qa_id": seed["qa_id"],
        "corpus_version": corpus_version,
        "doc_id": manifest_doc["doc_id"],
        "doc_path": seed["doc_path"],
        "canonical_path": manifest_doc["canonical_path"],
        "content_sha256": actual_sha,
        "question": seed["question"],
        "reference_answer": seed["reference_answer"],
        # Keep the two canonical Ragas names alongside the project-friendly
        # names so the JSONL can be adapted without lossy field renaming.
        "user_input": seed["question"],
        "reference": seed["reference_answer"],
        "reference_contexts": [item["text"] for item in contexts],
        "reference_context_ids": [item["evidence_id"] for item in contexts],
        "reference_context_spans": contexts,
        "source_spans": evidence,
        "question_type": seed["question_type"],
        "difficulty": seed["difficulty"],
        "table_dependent": bool(seed.get("table_dependent", False)),
        "image_dependent": bool(seed.get("image_dependent", False)),
        "answerable": True,
        "version_scope": seed.get("version_scope"),
        "split": "holdout" if seed["doc_path"] in HOLDOUT_DOC_PATHS else "tuning",
        "generation_method": "agent_authored_from_frozen_source",
        "review_status": seed.get("review_status", "agent_provisional_pending_human_review"),
        "review_notes": seed.get("review_notes", (
            "Evidence offsets and manifest hash were machine-validated. "
            "A human reviewer must still confirm wording, completeness, and table cells."
        )),
    }
    for field in (
        "qa_version", "question_language", "source_language", "fact_id",
        "topic_family", "source_scope", "reviewed_at", "reviewer",
        "agent_checks", "revision_notes",
    ):
        if field in seed:
            record[field] = seed[field]
    return record


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def build_report(
    records: list[dict[str, Any]],
    seed_count: int,
    manifest: dict[str, Any],
    output_path: Path,
) -> str:
    by_doc = collections.Counter(record["doc_path"] for record in records)
    by_type = collections.Counter(record["question_type"] for record in records)
    by_split = collections.Counter(record["split"] for record in records)
    table_count = sum(record["table_dependent"] for record in records)
    image_count = sum(record["image_dependent"] for record in records)
    unresolved = sum(
        1
        for record in records
        for span in record["source_spans"]
        if span["start_char"] < 0 or span["end_char"] <= span["start_char"]
    )

    doc_lines = [
        "| 文档 | 题数 | 调参/保留 |",
        "|---|---:|---|",
    ]
    for path in sorted(by_doc):
        split = "保留集" if path in HOLDOUT_DOC_PATHS else "调参集"
        doc_lines.append(f"| `{path}` | {by_doc[path]} | {split} |")

    type_lines = ["| 题型 | 题数 |", "|---|---:|"]
    for question_type, count in sorted(by_type.items()):
        type_lines.append(f"| `{question_type}` | {count} |")

    return f"""# 首轮 QA 评测集质量报告

> 生成日期：2026-10-09  
> 语料版本：`{manifest['corpus_version']}`  
> 状态：**代理初审完成，待人工内容审核；尚未运行检索或 Ragas 评分**

## 1. 结果概览

- QA seed：{seed_count} 条；已解析为 provisional QA：{len(records)} 条。
- 覆盖冻结语料：{len(by_doc)} / {manifest['included_count']} 份文档。
- 调参集：{by_split['tuning']} 条；保留集：{by_split['holdout']} 条。
- 表格依赖题：{table_count} 条；图片依赖题：{image_count} 条。
- 无法解析的证据区间：{unresolved} 条；每条题均包含至少一个冻结文档证据区间。
- 纳入文档 SHA-256 已逐条与 manifest 核对。

## 2. 当前生成方式

本批问题由 Codex 代理依据冻结 Markdown 原文编写，属于代理生成的问题候选，不是人工编写或真实用户日志。seed 记录源文档行范围；脚本将行范围解析为字符区间、证据文本和 heading。该解析脚本没有发起额外的模型 API 调用。

每条记录包含 Ragas 可用的 `user_input` 对应字段（`question`）、`reference` 对应字段（`reference_answer`）和 `reference_contexts`，并补充 `doc_id`、语料版本、SHA-256、证据 offset、题型、难度、表格/图片依赖和 split。

## 3. 文档覆盖

{chr(10).join(doc_lines)}

## 4. 题型覆盖

{chr(10).join(type_lines)}

## 5. 审核边界

当前 `review_status` 为 `agent_provisional_pending_human_review`，不能直接称为最终冻结 QA。人工审核需要逐题确认：

1. 问题是否自然、无歧义，并且确实属于该文档的知识范围；
2. 参考答案是否完整，是否添加了原文没有支持的主张；
3. 证据是否支持答案中的每个关键主张；
4. 表格题的表头、行列、单位和状态是否对应；
5. 题目是否依赖图片、链接目标或文档外的版本信息；
6. 同一事实的重复问法是否需要合并或标记为同一 fact cluster。

## 6. 划分规则

调参集和保留集按**完整文档**划分，避免同一文档的事实同时出现在两边。保留文档列表见 `qa_splits.json`。这不是最终统计显著性证明；文档数和题数仍然有限，后续应在 QA 人工审核后检查题型平衡和事实簇重复。

## 7. 下一步

1. 人工审核本文件列出的 44 条 provisional QA；修订或排除问题时保留审核理由。
2. 输出 `qa_reviewed.jsonl` 和审核日志，冻结 QA 版本。
3. 之后再实现 B/C 检索适配和批量 runner；当前没有 Ragas 依赖，也没有可用的模型凭据，因此本次不运行 LLM 评测。

输出文件：`{output_path}`。
"""


def build_review_log(records: list[dict[str, Any]]) -> str:
    lines = [
        "# 首轮 QA 审核日志",
        "",
        "> 当前为代理初审清单，所有记录均等待人工内容审核；不要将 `pending_human_review` 当作通过。",
        "",
        "| QA | 文档 | 题型 | 难度 | 表格依赖 | split | 机器校验 | 人工状态 |",
        "|---|---|---|---|---:|---|---|---|",
    ]
    for record in records:
        lines.append(
            "| `{qa_id}` | `{doc_path}` | `{question_type}` | `{difficulty}` | "
            "{table} | {split} | hash+offset passed | pending_human_review |".format(
                qa_id=record["qa_id"],
                doc_path=record["doc_path"],
                question_type=record["question_type"],
                difficulty=record["difficulty"],
                table="是" if record["table_dependent"] else "否",
                split="保留集" if record["split"] == "holdout" else "调参集",
            )
        )
    lines.extend(
        [
            "",
            "## 人工审核记录格式",
            "",
            "审核时为每条题补充：`reviewer`、`reviewed_at`、`decision`（keep/revise/drop）、"
            "`answer_ok`、`evidence_ok`、`question_ok`、`duplicate_fact_cluster` 和 `notes`。",
            "表格题还需记录表头、行、列、单位和脚注核对结果；图片引用文档的题目不能依赖图片不可见内容。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--splits", type=Path, default=DEFAULT_SPLITS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--review-log", type=Path, default=DEFAULT_REVIEW_LOG)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    seed_records = read_jsonl(args.seed)
    documents = {item["path"]: item for item in manifest["documents"] if item.get("included")}
    if len(documents) != manifest["included_count"]:
        raise ValueError("Manifest included_count does not match included document records")

    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for seed in seed_records:
        qa_id = seed.get("qa_id")
        if not qa_id or qa_id in seen_ids:
            raise ValueError(f"Duplicate or missing qa_id: {qa_id!r}")
        seen_ids.add(qa_id)
        doc_path = seed.get("doc_path")
        if doc_path not in documents:
            raise ValueError(f"{qa_id}: doc_path is not an included manifest document: {doc_path}")
        records.append(resolve_record(seed, documents[doc_path], manifest["corpus_version"]))

    if len(records) != len(seed_records):
        raise AssertionError("Resolved record count differs from seed count")

    write_jsonl(args.output, records)
    split_payload = {
        "schema_version": "0.1",
        "corpus_version": manifest["corpus_version"],
        "qa_status": "provisional_agent_review_pending_human_review",
        "split_unit": "document",
        "holdout_doc_paths": sorted(HOLDOUT_DOC_PATHS),
        "tuning_doc_paths": sorted(set(documents) - HOLDOUT_DOC_PATHS),
        "counts": {
            "total": len(records),
            "tuning": sum(record["split"] == "tuning" for record in records),
            "holdout": sum(record["split"] == "holdout" for record in records),
        },
        "leakage_policy": "Keep all questions from a document in one split; review related-topic documents for residual semantic leakage.",
    }
    args.splits.parent.mkdir(parents=True, exist_ok=True)
    args.splits.write_text(
        json.dumps(split_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        build_report(records, len(seed_records), manifest, args.output),
        encoding="utf-8",
    )
    args.review_log.parent.mkdir(parents=True, exist_ok=True)
    args.review_log.write_text(build_review_log(records), encoding="utf-8")
    print(
        json.dumps(
            {
                "records": len(records),
                "tuning": split_payload["counts"]["tuning"],
                "holdout": split_payload["counts"]["holdout"],
                "output": str(args.output),
                "report": str(args.report),
                "review_log": str(args.review_log),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
