#!/usr/bin/env python3
"""Build versioned, source-reviewed QA from explicit agent review decisions.

This script checks provenance and renders review materials. It does not
perform semantic review itself, sign off a human review, or call model APIs.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any

from prepare_eval_dataset import (
    DEFAULT_MANIFEST,
    DEFAULT_SEED,
    HOLDOUT_DOC_PATHS,
    PROJECT_ROOT,
    read_jsonl,
    resolve_record,
    sha256_file,
    write_jsonl,
)
from ragas_adapter import to_ragas_record


EVAL_DIR = PROJECT_ROOT / "pilot/data/eval"
ZH_DOCS = {
    "dbgpts/how-to-use-custom-skill.md", "installation/index.md",
    "application/graph_rag.md", "installation/integrations.md",
}
TOPICS = {
    "modules/smmf.md": "smmf", "application/advanced_tutorial/smmf.md": "smmf",
    "application/apps/chat_knowledge.md": "knowledge_base",
    "application/awel.md": "awel", "application/apps/app_manage.md": "app_management",
    "agents/modules/action/action.md": "agent_actions",
    "agents/introduction/tools.md": "agentic_tools", "modules/connections.md": "connections",
    "application/prompts.md": "prompts", "dbgpts/how-to-use-custom-skill.md": "custom_skills",
    "installation/index.md": "installation", "application/graph_rag.md": "graph_rag",
    "installation/integrations.md": "integrations",
}
EDITABLE_FIELDS = {
    "question", "reference_answer", "question_type", "difficulty",
    "evidence_line_ranges", "context_line_ranges", "table_dependent", "image_dependent",
}


def check_sources(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Check exact provenance, not the truth of an answer's claims."""
    identities: dict[str, tuple[str, int, int, str]] = {}
    span_count = 0
    context_count = 0
    table_questions = 0
    for qa in records:
        text = Path(qa["canonical_path"]).read_text(encoding="utf-8")
        for key in ("source_spans", "reference_context_spans"):
            for span in qa[key]:
                start, end = span["start_char"], span["end_char"]
                if not (0 <= start < end <= len(text)) or text[start:end] != span["text"]:
                    raise ValueError(f"{qa['qa_id']}: {key} does not reproduce source")
                identity = (qa["content_sha256"], start, end, span["text"])
                previous = identities.setdefault(span["evidence_id"], identity)
                if previous != identity:
                    raise ValueError(f"Conflicting evidence identity: {span['evidence_id']}")
                if re.search(r"!\[[^\]]*\]\(|<img\b|```|~~~", span["text"], re.I):
                    raise ValueError(f"{qa['qa_id']}: selected context contains image or fenced code")
                if key == "source_spans":
                    span_count += 1
                else:
                    context_count += 1
        if qa["question"] != qa["user_input"] or qa["reference_answer"] != qa["reference"]:
            raise ValueError(f"{qa['qa_id']}: field alias mismatch")
        if qa["reference_contexts"] != [s["text"] for s in qa["reference_context_spans"]]:
            raise ValueError(f"{qa['qa_id']}: reference context mismatch")

        if qa["table_dependent"]:
            table_questions += 1
            lines = text.splitlines()
            selected = {
                n for span in qa["reference_context_spans"]
                for n in range(span["line_start"], span["line_end"] + 1)
            }
            table_starts: list[int] = []
            for number in sorted(selected):
                if not lines[number - 1].lstrip().startswith("|"):
                    continue
                head = number
                while head > 1 and lines[head - 2].lstrip().startswith("|"):
                    head -= 1
                table_starts.append(head)
                if head not in selected or head + 1 not in selected:
                    raise ValueError(f"{qa['qa_id']}: missing table header at line {head}")
                separator = lines[head].replace(" ", "")
                if not re.fullmatch(r"\|(?::?-+:?\|)+", separator):
                    raise ValueError(f"{qa['qa_id']}: invalid table separator at line {head + 1}")
            if not table_starts:
                raise ValueError(f"{qa['qa_id']}: table-dependent question lacks a table")
    return {
        "document_hashes": "passed_by_resolver",
        "source_span_substring_checks": span_count,
        "reference_context_substring_checks": context_count,
        "evidence_identity_conflicts": 0,
        "table_header_checks": table_questions,
        "selected_contexts_with_images_or_fenced_code": 0,
        "semantic_verdict": "not_computed_by_script; see agent review decisions",
    }


def build_packet(records: list[dict[str, Any]], audits: list[dict[str, Any]], version: str) -> str:
    lines = [
        "# QA 回源复核与修订版 v2：逐题阅读材料", "",
        f"> QA 版本：`{version}`；{len(records)} 条覆盖 {len({q['doc_path'] for q in records})} 份冻结 Markdown。",
        "> 以下为 Codex 代理逐题回源复核，人工审核状态仍为待审核。", "",
        "所有答案以指定文档快照为准；这份材料不验证当前产品功能。英文文档用英文问答，中文文档用中文问答。", "",
        "完整表格可以作为审核上下文；`source_spans` 只标支持答案的区间。二者不同不能直接混用。表头计入选定证据时也必须实际召回；字符覆盖不能替代语义支持判断。", "",
        "原 14 条保留题已用于历史 TF-IDF 诊断，因此不再是从未查看的盲测集。当前划分只提供文档隔离。", "",
        "## 审核索引", "",
        "| QA | 文档 | 处理 | 语言 | 表格 | split |", "|---|---|---|---|---|---|",
    ]
    audit_by_id = {a["qa_id"]: a for a in audits}
    for qa in records:
        audit = audit_by_id[qa["qa_id"]]
        lines.append(
            f"| [{qa['qa_id']}](#{qa['qa_id']}) | `{qa['doc_path']}` | "
            f"{'修订' if audit['decision'] == 'revise' else '保留'} | {qa['question_language']} | "
            f"{'是' if qa['table_dependent'] else '否'} | {qa['split']} |"
        )
    for qa in records:
        audit = audit_by_id[qa["qa_id"]]
        lines.extend([
            "", f"## {qa['qa_id']}", "",
            f"**问题：** {qa['question']}", "", f"**参考答案：** {qa['reference_answer']}", "",
            f"文档：[{qa['doc_path']}]({qa['canonical_path']})；split：`{qa['split']}`；"
            f"题型：`{qa['question_type']}`；难度：`{qa['difficulty']}`（代理主观标记）。", "",
            f"**复核判断与修订理由：** {audit['notes']}", "",
            "**原文参考上下文：**", "",
        ])
        for span in qa["reference_context_spans"]:
            lines.extend([
                f"来源：[第 {span['line_start']}–{span['line_end']} 行]"
                f"({qa['canonical_path']}:{span['line_start']})；字符区间 "
                f"`[{span['start_char']}, {span['end_char']})`。", "", span["text"], "",
            ])
        ranges = ", ".join(f"{s['line_start']}–{s['line_end']}" for s in qa["source_spans"])
        lines.extend([
            f"**选定支持证据：** 原文第 {ranges} 行。坐标基于冻结后的规范 Markdown，单位为 Python Unicode 字符，左闭右开。", "",
            "**人工复核：** 待审核。可记录 keep / revise / drop、审核人、日期及理由。", "",
        ])
        if audit["decision"] == "revise":
            lines.extend([
                "<details><summary>查看修订前题目和修改字段</summary>", "",
                f"原问题：{audit['original_question']}", "",
                f"原答案：{audit['original_reference_answer']}", "",
                "修改字段：" + ", ".join(f"`{k}`" for k in audit["changes"]), "",
                "</details>", "",
            ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--original-seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--decisions", type=Path, default=EVAL_DIR / "qa_review_decisions_v2.json")
    parser.add_argument("--output-dir", type=Path, default=EVAL_DIR)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    review = json.loads(args.decisions.read_text(encoding="utf-8"))
    originals = read_jsonl(args.original_seed)
    decisions = {d["qa_id"]: d for d in review["decisions"]}
    original_ids = [q["qa_id"] for q in originals]
    if len(original_ids) != len(set(original_ids)):
        raise ValueError("Original QA IDs are not unique")
    if len(decisions) != len(review["decisions"]) or set(decisions) != set(original_ids):
        raise ValueError("Each original question must have exactly one review decision")
    if review["review_status"] != "agent_source_reviewed_pending_human_review":
        raise ValueError("This builder only records agent review, not human approval")
    docs = {d["path"]: d for d in manifest["documents"] if d.get("included")}
    if len(docs) != manifest["included_count"]:
        raise ValueError("Manifest included document count mismatch")
    version = review["qa_version"]
    seeds, records, audits = [], [], []
    for original in originals:
        decision = decisions[original["qa_id"]]
        changes = decision["changes"]
        if set(changes) - EDITABLE_FIELDS:
            raise ValueError(f"Unsupported revision fields: {set(changes) - EDITABLE_FIELDS}")
        if not decision["notes"].strip():
            raise ValueError(f"{original['qa_id']}: review note required")
        seed = {**original, **changes}
        doc_path = seed["doc_path"]
        if doc_path not in docs:
            raise ValueError(f"Not in frozen corpus: {doc_path}")
        language = "zh" if doc_path in ZH_DOCS else "en"
        topic = TOPICS.get(doc_path, {"q0004": "awel", "q0005": "agent_memory", "q0006": "graph_rag"}.get(seed["qa_id"], "release_notes"))
        agent_checks = {
            "question_scope": "passed_by_agent_reading",
            "reference_supported": "passed_by_agent_reading",
            "evidence_sufficient": "passed_by_agent_reading",
            "table_cells": "passed_by_agent_reading" if seed["table_dependent"] else "not_applicable",
            "external_image_or_link_dependency": "none_required_by_answer",
            "duplicate_fact": "reviewed; related questions are not statistically independent",
        }
        seed.update({
            "qa_version": version, "question_language": language, "source_language": language,
            "fact_id": f"{version}:{seed['qa_id']}", "topic_family": topic,
            "source_scope": f"Only the designated document {doc_path} in {manifest['corpus_version']}",
            "version_scope": seed.get("version_scope", f"Frozen snapshot {manifest['corpus_version']}"),
            "reviewed_at": review["reviewed_at"], "reviewer": review["reviewer"],
            "review_status": review["review_status"], "review_notes": decision["notes"],
            "revision_notes": decision["notes"], "agent_checks": agent_checks,
        })
        record = resolve_record(seed, docs[doc_path], manifest["corpus_version"])
        seeds.append(seed)
        records.append(record)
        audits.append({
            "qa_id": seed["qa_id"], "qa_version": version,
            "decision": "revise" if changes else "keep", "changes": changes,
            "original_question": original["question"],
            "original_reference_answer": original["reference_answer"],
            "original_evidence_line_ranges": original["evidence_line_ranges"],
            "notes": decision["notes"], "agent_checks": agent_checks,
            "reviewer": review["reviewer"], "reviewed_at": review["reviewed_at"],
            "human_review_status": "pending_human_review",
        })
    checks = check_sources(records)
    counts = {
        "total": len(records), "documents": len({q["doc_path"] for q in records}),
        "decisions": dict(collections.Counter(a["decision"] for a in audits)),
        "languages": dict(collections.Counter(q["question_language"] for q in records)),
        "splits": dict(collections.Counter(q["split"] for q in records)),
        "table_dependent": sum(q["table_dependent"] for q in records),
        "question_types": dict(collections.Counter(q["question_type"] for q in records)),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "seed": args.output_dir / "qa_seed_v2.jsonl",
        "qa": args.output_dir / "qa_source_reviewed_v2.jsonl",
        "audit": args.output_dir / "qa_audit_v2.jsonl",
        "ragas": args.output_dir / "ragas_input_v2.jsonl",
        "packet": args.output_dir / "qa_review_packet_v2.md",
        "report": args.output_dir / "qa_quality_report_v2.md",
    }
    write_jsonl(outputs["seed"], seeds)
    write_jsonl(outputs["qa"], records)
    write_jsonl(outputs["audit"], audits)
    write_jsonl(outputs["ragas"], [to_ragas_record(q) for q in records])
    outputs["packet"].write_text(build_packet(records, audits, version), encoding="utf-8")
    report = f"""# QA 回源复核 v2 质量报告

> 日期：{review['reviewed_at']}；QA 版本：`{version}`；语料版本：`{manifest['corpus_version']}`。
> 状态：代理逐题回源复核完成，人工内容审核待做。未进行 embedding、回答生成或 Ragas 评分。

## 结果

- 共 {counts['total']} 题，覆盖 {counts['documents']} 份冻结文档；内容保留 {counts['decisions'].get('keep', 0)} 题、修订 {counts['decisions'].get('revise', 0)} 题。全部记录补充 v2 元数据。
- 英文问答 {counts['languages'].get('en', 0)} 题，中文问答 {counts['languages'].get('zh', 0)} 题；问答与主要正文语言一致。
- 按文档划分：调参 {counts['splits'].get('tuning', 0)} 题、保留 {counts['splits'].get('holdout', 0)} 题；表格依赖 {counts['table_dependent']} 题，图片依赖 0 题。
- 精确证据区间 {checks['source_span_substring_checks']} 个、参考上下文区间 {checks['reference_context_substring_checks']} 个；全部能从规范原文逐字符还原。
- 14 份文档哈希由 resolver 核对；所有表格参考上下文含对应表头和分隔行，证据 ID 无冲突。

## 已修正的问题

1. q0005 与 q0016 原为同事实重复，q0005 换成发布说明中的 Agent Memory 类型；q0036/q0037 改为版本区别和环境准备，不再重复 q0035 的适用人群。保留原 ID 不代表问题语义保持不变，运行必须同时记录 QA 版本。
2. q0023/q0024/q0027/q0028 补齐表头。q0028 审核上下文展示完整表格，支持证据只标表头及四个符合条件的行，其他 No 行不计入证据分母。
3. 13 条中文文档题改为中文；q0034 的证据不再包含图片引用。第一轮仍只考查正文与表格中的文字。
4. q0032 补 .skill 格式的原文证据；q0039 补全各图捕获的关系与节点；q0024 明确只读探索不能修改数据。
5. q0006 只问有明确文本支持的摘要与检索功能，不用未复现的成本声明出题；q0009 明确 words 是该文档的说法，并非实验单位或现行实现验证。
6. Connections 与集成总览对 Oracle、OceanBase 等支持状态不同；Graph RAG 说明与集成总览对图存储的描述也不同。题目明确指定文档或表，答案仅对该快照有效；后续上下文必须保留来源标签。
7. 证据 ID 改为文档 ID + 完整内容哈希 + 字符起止位置；不同问题引用同一真实区间可以共享 ID，不同区间不能冲突。
8. 出题来源更正为 Codex 代理编写，不是人工编写或真实用户日志。脚本仅解析和校验，没有发起额外模型 API 调用。

## 证据与参考上下文

`source_spans` 是选定的答案支持区间；`reference_context_spans` / `reference_contexts` 可以额外含完整表格和周边说明，供审阅和评审模型读取。计算原文覆盖时使用前者，且 gold 与召回区间都先取并集去重。其长度是字符单位，不是分块参数使用的 token 单位。

选定证据不是所有可能的有效证据。例如 v0.6.0 发布说明中部分事实被重复描述；召回替代原文可以语义正确但未覆盖标注区间。严格覆盖仅作辅助诊断，仍需 Ragas 语义支持指标与必要人工核对。整行字符覆盖也不能保证每个关键单元格的含义正确。

## 划分与适用边界

- 保持 30/14 的原文档划分，未将一份文档拆到两边；相关主题仍可能跨文档，44 题不是 44 个完全独立统计样本。`fact_id` 是追踪标识，不是独立性认证。
- 原保留题已用于历史 TF-IDF 诊断、结果也已查看。它只是文档隔离的开发评测集，不能再称从未暴露的盲测集。正式最终验证应另加未参与调参的新文档/新问题并提前锁定。
- 本集全为单文档可回答题，没有真实业务查询日志、无答案题、多文档证据题、图片题、围栏代码题或复杂 PDF 表格题。题型与难度由代理标记，尚无用户代表性验证。
- 人工复核仍待完成；脚本的 hash / offset / table-header 检查不能证明语义判断正确。逐题内容判断见 `qa_audit_v2.jsonl`，可阅读材料见 `qa_review_packet_v2.md`。
- v1 seed/provisional/splits/离线结果保持为历史记录，v2 未评分；不要混合版本比较。

## 后续顺序

1. 依据逐题阅读材料完成必要的人工修订，并在独立新版本中记录审核人、日期与冻结哈希；实现方面可继续修来源映射和 token 计量。
2. 修复 B/C 分块来源映射以及覆盖区间重复计数，再做可解释的链路诊断。
3. 正式实验采用 token 作为 chunk_size / overlap 单位；先固定 embedding 模型及对应 tokenizer，把标题注入算入最终块长，再统计语料长度和确定候选参数。
4. 接入统一 embedding / 检索 / 回答 / Ragas 设置，先在调参集运行初始比较，再搜索参数；最终使用新的独立保留材料验证。

## 重建产物

在项目根目录运行（仅使用标准库，不下载模型）：

```sh
/opt/homebrew/opt/python@3.11/bin/python3.11 pilot/rag_pipeline/review_eval_dataset.py
```

输入是冻结语料 manifest、原始 seed 和逐题 `qa_review_decisions_v2.json`；版本 manifest 保存输入及输出 SHA-256。重建脚本不执行语义审核，也不会把待人工审核状态升级为通过。
"""
    outputs["report"].write_text(report, encoding="utf-8")
    version_manifest = {
        "schema_version": "1.0", "qa_version": version,
        "corpus_version": manifest["corpus_version"], "review_status": review["review_status"],
        "generation_method": review["generation_method"], "counts": counts,
        "machine_checks": checks, "split_unit": "document",
        "holdout_doc_paths": sorted(HOLDOUT_DOC_PATHS),
        "tuning_doc_paths": sorted(set(docs) - HOLDOUT_DOC_PATHS),
        "split_qa_ids": {
            split: [q["qa_id"] for q in records if q["split"] == split]
            for split in ("tuning", "holdout")
        },
        "holdout_has_prior_diagnostic_exposure": True,
        "inputs": {str(p): sha256_file(p) for p in (args.manifest, args.original_seed, args.decisions)},
        "outputs": {str(p): sha256_file(p) for p in outputs.values()},
        "builder_sha256": sha256_file(Path(__file__)),
        "resolver_sha256": sha256_file(PROJECT_ROOT / "pilot/rag_pipeline/prepare_eval_dataset.py"),
        "adapter_sha256": sha256_file(PROJECT_ROOT / "pilot/rag_pipeline/ragas_adapter.py"),
    }
    manifest_path = args.output_dir / "qa_version_manifest_v2.json"
    manifest_path.write_text(json.dumps(version_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"version": version, "counts": counts, "checks": checks, "packet": str(outputs['packet'])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
