#!/usr/bin/env python3
"""Freeze controlled answers and agent labels before evaluating any judge."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "pilot/data/eval"


def main() -> None:
    questions = {r["qa_id"]: r for r in [json.loads(l) for l in (EVAL / "qa_text_tuning_v3.jsonl").read_text().splitlines()]}
    # No target label is sent to the judge; no B/C retrieval result is used here.
    specs = [
        ("definition_paraphrase", "q0007", "pass", "A Knowledge Space in DB-GPT is a document space for a type of knowledge, where documents of that same type can be uploaded.", True),
        ("definition_wrong_type", "q0007", "fail", "A Knowledge Space in DB-GPT is a relational database table that stores SQL query histories.", False),
        ("steps_complete", "q0012", "pass", None, True),
        ("steps_wrong_alternative", "q0012", "fail", "Click + at the Streaming LLM Operator's HOContext input and select Knowledge Operator. Alternatively, drag Knowledge Operator and connect it to Streaming LLM Operator. Select the knowledge space in Knowledge Space Name.", True),
        ("steps_missing_drag", "q0012", "fail", "Click + at the Streaming LLM Operator's second input, HOContext, and select Knowledge Operator. Then select the knowledge space in Knowledge Space Name.", True),
        ("numbers_and_unit_correct", "q0009", "pass", None, False),
        ("unit_wrong", "q0009", "fail", "The Chat Knowledge Base document states a default chunk size of 512 tokens and an overlap of 50 tokens.", True),
        ("number_wrong", "q0009", "fail", "The Chat Knowledge Base document states a chunk size of 800 words and overlap of 100 words.", False),
        ("publication_condition_correct", "q0018", "pass", None, False),
        ("publication_condition_missing", "q0018", "fail", "Other users can discover and use an application immediately after it has been created, even if it has not been published.", False),
        ("three_methods_no_optional_format", "q0032", "pass", "DB-GPT 的三种方式是使用内置 skill-creator 从零创建、上传 zip 包导入、通过 GitHub 链接导入。", True),
        ("three_methods_with_optional_format", "q0032", "pass", "DB-GPT 支持用内置 skill-creator 创建，上传打包好的 .zip 或 .skill 文件导入，以及通过 GitHub 仓库链接导入。", False),
        ("list_missing_method", "q0032", "fail", "DB-GPT 支持两种方式：用内置 skill-creator 从零创建，以及上传 zip 包导入。", False),
        ("github_valid_extra", "q0034", "pass", "进入 DB-GPT 的 Skills 页面，点击 GitHub 导入并粘贴 skill 所在仓库链接；系统会自动拉取仓库内容并完成导入，成功后即可使用。该方式适合社区或团队共享的 skill。", True),
        ("github_unsupported_guarantee", "q0034", "fail", "进入 Skills 页面，点击 GitHub 导入并粘贴仓库链接，系统自动拉取并导入，成功后即可使用。它还会永久自动同步仓库后续更新，无需任何配置。", False),
        ("creator_complete", "q0033", "pass", None, False),
        ("creator_missing_packaging", "q0033", "fail", "skill-creator 会分析需求并规划结构，生成包含元数据和执行指令的 SKILL.md，创建所需脚本、参考文档和资源文件。", True),
        ("prompt_list_complete", "q0030", "pass", None, False),
        ("prompt_list_wrong_member", "q0030", "fail", "四类是 AGENT、SCENE、NORMAL 和 SYSTEM。", False),
        ("integration_complete", "q0042", "pass", None, False),
        ("integration_missing_group", "q0042", "fail", "覆盖数据源与 RAG 存储两类集成，帮助接入数据库和数仓。", False),
    ]
    rows = []
    for name, qid, expected, response, detailed in specs:
        q = questions[qid]
        source = Path(q["canonical_path"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != q["content_sha256"]:
            raise ValueError("Source changed before calibration dataset freeze")
        rows.append({"case_id": name, "qa_id": qid, "question": q["question"],
                     "reference": q["reference"], "grading_notes": q["grading_notes"],
                     "source_context": source.read_text(), "content_sha256": q["content_sha256"],
                     "doc_path": q["doc_path"], "response": response or q["reference"],
                     "expected_label": expected, "label_origin": "agent_authored_control_from_frozen_source",
                     "review_status": "pending_human_review", "detailed_ragas_comparison": detailed,
                     "diagnostic_group": "known_M3_failure_family" if qid in ["q0007", "q0012", "q0032", "q0034"] else "additional_condition_list_parameter_checks"})
    path = EVAL / "judge_calibration_v1.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n")
    manifest = {"dataset_id": "judge_calibration_2026-10-09_v1", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "cases": len(rows), "positive": sum(r["expected_label"] == "pass" for r in rows),
                "negative": sum(r["expected_label"] == "fail" for r in rows),
                "detailed_cases": sum(r["detailed_ragas_comparison"] for r in rows),
                "qa_sha256": hashlib.sha256((EVAL / "qa_text_tuning_v3.jsonl").read_bytes()).hexdigest(),
                "review_status": "agent_labeled_pending_human_review",
                "purpose": "Controlled implementation/calibration diagnostics, not human alignment certification or a chunking holdout",
                "label_policy": "No target label included in judge input. Criteria-based pass requires all core facts/relations and no unsupported assertions; optional source-supported detail allowed."}
    (EVAL / "judge_calibration_v1_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k:manifest[k] for k in ["cases", "positive", "negative", "detailed_cases"]}))


if __name__ == "__main__":
    main()
