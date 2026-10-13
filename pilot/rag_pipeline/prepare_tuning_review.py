#!/usr/bin/env python3
"""Build an auditable text-only tuning revision from explicit agent decisions.

No model calls. Original QA and old holdout rows are never rewritten. Required
facts/relations are project review annotations, not standard Ragas columns.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "pilot/data/eval"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parent = EVAL / "qa_text_only_v2.jsonl"
    source_scope = json.loads((EVAL / "qa_text_only_v2_manifest.json").read_text())
    if sha(parent) != source_scope["qa_sha256"]:
        raise ValueError("Parent QA does not match its scope hash")
    decision_path = EVAL / "qa_tuning_review_decisions_v3.json"
    review = json.loads(decision_path.read_text())
    questions = [json.loads(line) for line in parent.read_text().splitlines()]
    selected = [q for q in questions if q["split"] == "tuning"]
    decisions = review["decisions"]
    if set(decisions) != {q["qa_id"] for q in selected}:
        raise ValueError("Every tuning question must have exactly one decision")
    rows, lines = [], ["# 正文调参 QA v3：逐题参考与必答事实核对", "",
                       "2026-10-09；Codex 代理回源核对，人工审核待完成。24 道 tuning 题，问题、ID、split 和原文版本不变；35 题父集与旧保留题保留。", "",
                       "必答事实与关系是项目补充标注，不是 Ragas 强制字段。参考答案只包含问题要求的内容；有依据的额外说明不自动视为错误。证据字符覆盖继续仅作辅助。", ""]
    for original in selected:
        row = copy.deepcopy(original)
        choice = decisions[row["qa_id"]]
        source = Path(row["canonical_path"])
        text = source.read_text()
        if sha(source) != row["content_sha256"]:
            raise ValueError(f"Source version changed: {row['qa_id']}")
        for span in row["source_spans"] + row["reference_context_spans"]:
            if text[span["start_char"]:span["end_char"]] != span["text"]:
                raise ValueError(f"Evidence mismatch: {row['qa_id']}")
        original_spans = copy.deepcopy(row["source_spans"])
        primary = choice.get("primary_span_indices", list(range(len(original_spans))))
        if not primary or any(not 0 <= i < len(original_spans) for i in primary):
            raise ValueError("Invalid primary evidence indices")
        row["qa_version_original"] = row["qa_version"]
        row["qa_version"] = review["review_version"]
        row["reference_original"] = row["reference_answer"]
        row["source_spans_original"] = original_spans
        row["source_spans"] = [original_spans[i] for i in primary]
        row["optional_source_spans"] = [s for i, s in enumerate(original_spans) if i not in primary]
        row["reference_contexts_original"] = row["reference_contexts"]
        row["reference_context_spans"] = copy.deepcopy(row["source_spans"])
        row["reference_contexts"] = [s["text"] for s in row["reference_context_spans"]]
        row["reference_context_ids"] = [s["evidence_id"] for s in row["reference_context_spans"]]
        row["reference"] = row["reference_answer"] = choice["reference"]
        row["review_status"] = review["review_status"]
        row["reviewer"] = review["reviewer"]
        row["reviewed_at"] = "2026-10-09"
        row["reference_review"] = choice
        row["required_facts"] = [{"fact_id": f"{row['qa_id']}:f{i:02d}", "text": fact,
                                  "source_evidence_ids": [s["evidence_id"] for s in row["source_spans"]]}
                                 for i, fact in enumerate(choice["required_facts"], 1)]
        row["required_relations"] = choice.get("required_relations", [])
        row["grading_notes"] = json.dumps({"required_facts": choice["required_facts"],
                                           "required_relations": row["required_relations"],
                                           "scope": choice.get("scope_notes", ""),
                                           "optional": choice.get("optional_notes", "")}, ensure_ascii=False)
        rows.append(row)
        lines.extend([f"## {row['qa_id']} · {row['question_language']} · {row['question_type']}", "",
                      f"来源：`{row['doc_path']}`；原文 SHA256：`{row['content_sha256']}`", "",
                      f"**问题：**{row['question']}", "", f"**旧参考：**{row['reference_original']}", "",
                      f"**新参考：**{row['reference']}", "", "**必答事实：**", ""])
        lines.extend(f"- {fact['fact_id']}：{fact['text']}" for fact in row["required_facts"])
        for label, value in (("必答关系", row["required_relations"]), ("范围说明", choice.get("scope_notes")),
                             ("可选信息", choice.get("optional_notes")), ("修改原因", choice.get("change_reason", "补全主语/文档范围，或保持已有完整表达；未新增事实。"))):
            if value:
                lines.extend(["", f"**{label}：**" + ("；".join(value) if isinstance(value, list) else value)])
        for label, spans in (("主要证据", row["source_spans"]), ("可选证据", row["optional_source_spans"])):
            if not spans:
                continue
            lines.extend(["", f"**{label}：**", ""])
            for span in spans:
                lines.extend([f"第 {span['line_start']}–{span['line_end']} 行；字符 [{span['start_char']}, {span['end_char']})：", "",
                              "```text", span["text"], "```", ""])
        lines.extend(["人工确认：待完成。", ""])
    output = EVAL / "qa_text_tuning_v3.jsonl"
    output.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n")
    packet = EVAL / "qa_tuning_review_packet_v3.md"
    packet.write_text("\n".join(lines))
    manifest = {"scope_id": review["review_version"], "corpus_version": source_scope["corpus_version"],
                "qa_file": str(output.relative_to(ROOT)), "qa_sha256": sha(output),
                "parent_qa_file": str(parent.relative_to(ROOT)), "parent_qa_sha256": sha(parent),
                "decision_file": str(decision_path.relative_to(ROOT)), "decision_sha256": sha(decision_path),
                "generator_sha256": sha(Path(__file__)), "review_packet_sha256": sha(packet),
                "review_status": review["review_status"], "split": "tuning", "qa_count": len(rows),
                "selected_qa_ids": [r["qa_id"] for r in rows],
                "language_counts": dict(Counter(r["question_language"] for r in rows)),
                "question_type_counts": dict(Counter(r["question_type"] for r in rows)),
                "source_documents": len({r["doc_id"] for r in rows}),
                "required_fact_count": sum(len(r["required_facts"]) for r in rows),
                "scope_refined_ids": [q for q, d in decisions.items() if "change_reason" in d],
                "policies": ["Original question/ID/split/source hashes unchanged", "Original parent QA and old holdout retained",
                             "Required facts reviewed from source, not chosen to favor B/C scores", "Agent review is not human certification"]}
    (EVAL / "qa_text_tuning_v3_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: manifest[k] for k in ("scope_id", "qa_count", "language_counts", "source_documents", "required_fact_count", "scope_refined_ids")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
