#!/usr/bin/env python3
"""Compute ordinary document-set P/R from saved contexts and existing gold IDs.

This is a post-hoc source diagnostic, not chunk/evidence relevance evaluation.
It uses no models, does not read holdout questions, and preserves original runs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRID = ROOT / "pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3"
VIEWS = ("fixed_local_token_budget", "fixed_top_k")
VARIANTS = ("source_named", "source_title_omitted")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID)
    args = parser.parse_args()
    grid = args.grid.resolve()
    output = grid / "source_document_metrics_v1"
    inputs = {}

    def tracked(path):
        value = read(path)
        inputs[str(path.relative_to(ROOT))] = sha(path)
        return value

    manifest = tracked(grid / "grid_manifest.json")
    run_candidates = defaultdict(dict)
    for candidate in manifest["candidate_configs"]:
        run_candidates[candidate["run_id"]][candidate["strategy"]] = candidate["candidate_id"]
    all_qa = None
    rows = []
    for run_id, candidates in run_candidates.items():
        run = grid / "runs" / run_id
        config = tracked(run / "config.resolved.json")
        if config["split"] != "tuning":
            raise ValueError("Only tuning questions may be loaded in this diagnostic")
        qa_path = ROOT / config["qa_file"]
        if sha(qa_path) != config["qa_sha256"]:
            raise ValueError(f"QA hash changed: {qa_path}")
        inputs[str(qa_path.relative_to(ROOT))] = sha(qa_path)
        source_qa = [json.loads(line) for line in qa_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        selected = tracked(run / "selected_qa.json")
        qa = {q["qa_id"]: q for q in selected}
        if len(qa) != len(selected) or set(qa) != set(config["qa_ids"]):
            raise ValueError(f"Duplicate or unexpected QA IDs in {run_id}")
        source_by_id = {q["qa_id"]: q for q in source_qa}
        if any(q != source_by_id[qid] for qid, q in qa.items()):
            raise ValueError(f"Selected QA differs from frozen tuning file: {run_id}")
        if all_qa is not None and qa != all_qa:
            raise ValueError("Runs have different question annotations")
        all_qa = qa
        chunks = {}
        for strategy in candidates:
            stored = tracked(run / f"{strategy}_chunks.json")
            chunks[strategy] = {c["chunk_id"]: c for c in stored}
            if len(chunks[strategy]) != len(stored):
                raise ValueError(f"Duplicate chunk IDs in {run_id}/{strategy}")
        known_docs = {c["doc_id"] for items in chunks.values() for c in items.values()}
        records = tracked(run / "results.json")
        expected = {(strategy, qid, view) for strategy in candidates for qid in qa for view in VIEWS}
        seen = set()
        for record in records:
            key = record["strategy"], record["qa_id"], record["view"]
            if key in seen or key not in expected:
                raise ValueError(f"Duplicate/unexpected result: {run_id}/{key}")
            seen.add(key)
            question = qa[record["qa_id"]]
            if record["answer_status"] != "ok" or record["pair_group_id"] != question["pair_group_id"]:
                raise ValueError(f"Incomplete or mismatched result: {run_id}/{key}")
            if record["question_variant"] != question["question_variant"]:
                raise ValueError(f"Question variant mismatch: {run_id}/{key}")
            gold = set(question["reference_context_ids"])
            if not gold or not gold.issubset(known_docs):
                raise ValueError(f"Missing or unresolvable gold source IDs: {run_id}/{key}")
            used = record["contexts_used"]
            # retrieved_context_ids are CHUNK IDs; never compare them to gold DOC IDs.
            if record["retrieved_context_ids"] != [c["chunk_id"] for c in used]:
                raise ValueError(f"Selected chunk ID list mismatch: {run_id}/{key}")
            for context in used:
                original = chunks[record["strategy"]].get(context["chunk_id"])
                if original is None or original["doc_id"] != context["doc_id"]:
                    raise ValueError(f"Chunk-to-document mapping mismatch: {run_id}/{key}")
            retrieved = {c["doc_id"] for c in used}
            tp = len(gold & retrieved)
            rows.append({
                "candidate_id": candidates[record["strategy"]], "run_id": run_id,
                "result_id": record["result_id"], "qa_id": record["qa_id"],
                "pair_group_id": record["pair_group_id"], "variant": record["question_variant"],
                "view": record["view"], "gold_doc_ids": sorted(gold),
                "retrieved_doc_ids": sorted(retrieved), "matched_doc_ids": sorted(gold & retrieved),
                "gold_document_count": len(gold), "retrieved_document_count": len(retrieved),
                "matched_document_count": tp, "selected_chunk_count": len(used),
                "document_precision": tp / len(retrieved) if retrieved else 0.0,
                "document_recall": tp / len(gold),
            })
        if seen != expected:
            raise ValueError(f"Missing final results: {run_id}")

    summaries = []
    for candidate in manifest["candidate_configs"]:
        for view in VIEWS:
            for variant in ("all", *VARIANTS):
                subset = [r for r in rows if r["candidate_id"] == candidate["candidate_id"]
                          and r["view"] == view and (variant == "all" or r["variant"] == variant)]
                groups = defaultdict(list)
                for row in subset:
                    groups[row["pair_group_id"]].append(row)
                expected_variants = set(VARIANTS) if variant == "all" else {variant}
                if not groups or any({r["variant"] for r in rs} != expected_variants for rs in groups.values()):
                    raise ValueError(f"Unbalanced variants: {candidate['candidate_id']}/{view}/{variant}")
                tp = sum(r["matched_document_count"] for r in subset)
                nr = sum(r["retrieved_document_count"] for r in subset)
                ng = sum(r["gold_document_count"] for r in subset)
                summaries.append({
                    "candidate_id": candidate["candidate_id"], "view": view, "variant": variant,
                    "query_count": len(subset), "fact_group_count": len(groups),
                    "document_precision_macro": mean(mean(r["document_precision"] for r in rs) for rs in groups.values()),
                    "document_recall_macro": mean(mean(r["document_recall"] for r in rs) for rs in groups.values()),
                    "document_precision_micro": tp / nr if nr else 0.0,
                    "document_recall_micro": tp / ng,
                    "mean_retrieved_documents": mean(r["retrieved_document_count"] for r in subset),
                    "mean_selected_chunks": mean(r["selected_chunk_count"] for r in subset),
                    "sum_matched_documents": tp, "sum_retrieved_documents": nr, "sum_gold_documents": ng,
                })

    if any(sha(ROOT / name) != digest for name, digest in inputs.items()):
        raise ValueError("An input changed during computation")
    output.mkdir(exist_ok=True)
    save(output / "per_query.json", rows)
    save(output / "metrics.json", summaries)
    with (output / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    save(output / "provenance.json", {
        "scope": "post_hoc_document_set_diagnostic_from_existing_gold_source_annotations",
        "retrieval_stage": "actual_final_context_after_chunk_selection_and_packing",
        "relevance_unit": "deduplicated_source_document", "aggregation": "paired_fact_group_macro",
        "empty_retrieved_set_precision": 0.0, "gold_label_completeness": "not_exhaustively_audited_for_alternative_sources",
        "candidate_count": len(manifest["candidate_configs"]), "record_count": len(rows),
        "summary_count": len(summaries), "gold_document_counts": sorted({r["gold_document_count"] for r in rows}),
        "script_sha256": sha(Path(__file__)), "input_sha256": inputs,
        "new_model_calls": 0, "holdout_questions_loaded": False, "original_results_modified": False,
        "sources": [
            "https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-unranked-retrieval-sets-1.html",
            "https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/",
            "https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/",
        ],
    })
    report = [
        "# 普通文档级 Precision / Recall：现有来源标注诊断 v1", "",
        "2026-10-13。事后补充诊断，原四项 Ragas 主指标、原始 run 与评分保持不变。", "",
        "## 计算口径", "",
        "每题 G 是 `reference_context_ids` 中的来源文档 ID 集合；R 是实际送入回答模型的 "
        "`contexts_used[*].doc_id` 去重集合。`retrieved_context_ids` 是块 ID，必须映射到同一文档 ID 空间。", "",
        "- 文档 Precision = |G ∩ R| / |R|；空 R 按 0 记。",
        "- 文档 Recall = |G ∩ R| / |G|；本次所有 G 均非空且恰有一份文档。",
        "- 两个问法先在事实组内平均，再对 23 个事实组平均；同时另报两问法及 micro 汇总。",
        "- micro 是跨题累计的命中数除以累计返回数/标注数，不对跨题的文档去重。",
        "- 使用最终上下文；不是候选池前 20 的得分，也不是块 Precision@5 或排名 AP。",
        "- top-5 和 2560 token 预算视图分别统计；预算视图返回块数可变。", "",
        "## 结果", "",
        "全部 18 个身份、两种视图均已计算。下表是合并两种问法的文档集合宏平均。", "",
    ]
    for view in VIEWS:
        report += [f"### {view}", "", "| 配置 | 文档 Precision | 文档 Recall | 平均文档数 | 平均块数 |",
                   "|---|---:|---:|---:|---:|"]
        for row in summaries:
            if row["view"] == view and row["variant"] == "all":
                report.append(f"| {row['candidate_id']} | {row['document_precision_macro']:.2%} | "
                              f"{row['document_recall_macro']:.2%} | {row['mean_retrieved_documents']:.2f} | "
                              f"{row['mean_selected_chunks']:.2f} |")
        report.append("")
    report += [
        "## 如何解释", "",
        "这是按现有 gold 来源标注得到的文档级指标。它衡量选中上下文是否来自指定来源，"
        "同一文档中与问题无关的条款也会记为该来源命中。单来源题的 Recall 等于来源命中率；"
        "100% 不表示找全条款、不表示参考事实完整、不表示回答正确。", "",
        "标注只指定当前参考答案来源，未穷尽其他文档中的替代支持证据。"
        "因此未标注来源不能直接断言在语义上无关；Precision 受现有来源标注和文档去重影响。"
        "例如原问题明确指定一部法律时其他来源通常不满足该来源约束；省略法规名的问题尤其要审查替代来源。", "",
        "同一来源的五个块去重后可能只有一份文档；即使其中四块与问题无关，文档 Precision 仍可能为 100%。"
        "块数、块大小、预算和文档合并会影响此诊断，不能仅按它选分块策略或推断某方案噪声更少。", "",
        "要评测具体证据的普通 P/R，需要定义固定、与分块配置无关的证据单元（例如条款），"
        "核对必需和替代证据、标注相关性，并明确部分覆盖的处理规则。"
        "现有 `source_spans` 可作起点，不能未经审核把任意区间交叠当成语义相关。"
        "字符覆盖辅助指标继续单独解释，不能改名为普通 Recall。", "",
        "## 产物与重现", "",
        "`metrics.json/.csv` 含 108 行分配置/视图/问法汇总；`per_query.json` 保存 1,656 条原始"
        "集合、分子分母和分数；`provenance.json` 保存输入与脚本哈希。未调用模型、未读取留出题、未修改原结果。", "",
        "```bash", "pilot/.venv-rag-eval/bin/python pilot/rag_pipeline/compute_source_document_metrics.py", "```", "",
        "## 定义依据", "",
        "普通集合 P/R 定义见 [Stanford IR 教材](https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-unranked-retrieval-sets-1.html)。"
        "Ragas 也提供不依赖 LLM 的 [IDBasedContextPrecision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/) "
        "和 [IDBasedContextRecall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)。"
        "本脚本直接计算集合公式，不声称调用了 Ragas ID 指标。Ragas 原 LLM ContextPrecision 的排名式评判口径与此不同。", "",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({"output": str(output), "records": len(rows), "summaries": len(summaries),
                      "representatives": [r for r in summaries if r["variant"] == "all"
                                          and r["candidate_id"] in {"C_structure_s512_o0", "B_recursive_s256_o51"}]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
