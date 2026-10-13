#!/usr/bin/env python3
"""Add positional character IoU to the frozen 18-config retrieval comparison.

This consumes existing tuning-only source-span metrics. It calls no models,
does not open holdout QA, and preserves the original experiment artifacts.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRID = ROOT / "pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3"
VIEWS = ("fixed_local_token_budget", "fixed_top_k")
METRICS = (
    "chunk_precision_overlap", "chunk_recall_overlap",
    "evidence_char_precision", "evidence_char_recall", "evidence_char_iou",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID)
    args = parser.parse_args()
    grid = args.grid.resolve()
    source = grid / "evidence_chunk_mapping_v1"
    paths = [source / name for name in ("per_query.json", "metrics.json", "provenance.json")]
    hashes = {str(path.relative_to(ROOT)): sha(path) for path in paths}
    source_rows, source_summaries, provenance = (
        json.loads(path.read_text(encoding="utf-8")) for path in paths
    )
    if provenance["holdout_questions_loaded"] or provenance["new_model_calls"]:
        raise ValueError("Expected the existing offline tuning-only evidence mapping")
    for relative, expected in provenance["input_sha256"].items():
        if sha(ROOT / relative) != expected:
            raise ValueError(f"Frozen mapping input changed: {relative}")

    candidates = sorted({row["candidate_id"] for row in source_rows})
    if len(candidates) != 18 or len(source_rows) != 1656:
        raise ValueError("Expected all 18 candidates and 1,656 candidate/query/view rows")
    seen = set()
    per_query = []
    for row in source_rows:
        key = (row["candidate_id"], row["qa_id"], row["view"])
        if key in seen or row["view"] not in VIEWS:
            raise ValueError(f"Invalid or duplicate row: {key}")
        seen.add(key)
        gold = int(row["gold_evidence_chars"])
        retrieved = int(row["retrieved_source_chars_including_overlap"])
        # Recover the integer intersection from the saved exact coverage ratio;
        # check independently against both existing P and R denominators.
        covered = round(gold * row["evidence_char_recall"])
        if gold <= 0 or retrieved < 0 or not 0 <= covered <= min(gold, retrieved):
            raise ValueError(f"Invalid character counts: {key}")
        precision = covered / retrieved if retrieved else 0.0
        if (abs(covered / gold - row["evidence_char_recall"]) > 1e-12
                or abs(precision - row["evidence_char_precision"]) > 1e-12):
            raise ValueError(f"Intersection reconstruction differs from saved P/R: {key}")
        missing = gold - covered
        denominator = retrieved + missing
        iou = covered / denominator
        if not 0 <= iou <= min(precision, covered / gold) + 1e-12:
            raise ValueError(f"Invalid IoU: {key}")
        per_query.append({
            **row,
            "covered_gold_chars": covered,
            "missing_gold_chars": missing,
            "evidence_char_iou": iou,
        })

    lookup = {(row["candidate_id"], row["view"]): row for row in source_summaries}
    summaries = []
    for view in VIEWS:
        for candidate in candidates:
            subset = [row for row in per_query
                      if row["candidate_id"] == candidate and row["view"] == view]
            groups = defaultdict(list)
            for row in subset:
                groups[row["pair_group_id"]].append(row)
            if len(subset) != 46 or len(groups) != 23 or any(len(pair) != 2 for pair in groups.values()):
                raise ValueError(f"Expected 23 paired fact groups: {candidate}/{view}")

            def group_values(metric):
                return [mean(row[metric] for row in pair) for pair in groups.values()]

            result = {
                "candidate_id": candidate, "view": view,
                "query_count": len(subset), "fact_group_count": len(groups),
                **{f"{metric}_macro": mean(group_values(metric)) for metric in METRICS},
                "evidence_char_iou_fact_group_std": pstdev(group_values("evidence_char_iou")),
                "mean_retrieved_chunks": mean(group_values("retrieved_chunk_count")),
                "mean_retrieved_source_chars_including_overlap": mean(group_values("retrieved_source_chars_including_overlap")),
                "all_gold_spans_fully_covered_rate": mean(group_values("all_gold_spans_fully_covered")),
            }
            old = lookup[(candidate, view)]
            for metric in METRICS[:-1]:
                if abs(result[f"{metric}_macro"] - old[f"{metric}_macro"]) > 1e-12:
                    raise ValueError(f"Original P/R summary changed: {candidate}/{view}/{metric}")
            summaries.append(result)

    for view in VIEWS:
        subset = [row for row in summaries if row["view"] == view]
        for row in subset:
            objectives = ("evidence_char_precision_macro", "evidence_char_recall_macro")
            row["on_character_pr_frontier"] = not any(
                all(other[key] >= row[key] for key in objectives)
                and any(other[key] > row[key] for key in objectives)
                for other in subset
            )
            row["iou_display_rank"] = 1 + sum(
                other["evidence_char_iou_macro"] > row["evidence_char_iou_macro"] for other in subset
            )

    if any(sha(ROOT / relative) != expected for relative, expected in hashes.items()):
        raise ValueError("An input changed during the comparison")
    out = grid / "retrieval_selection_v1"
    out.mkdir(exist_ok=True)
    save(out / "per_query.json", per_query)
    save(out / "metrics.json", summaries)
    with (out / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    save(out / "provenance.json", {
        "scope": "posthoc_tuning_retrieval_comparison_with_character_iou",
        "candidate_count": len(candidates), "query_rows": len(per_query), "summary_rows": len(summaries),
        "character_iou": "unique covered gold chars / (retrieved source chars including duplicates + uncovered unique gold chars)",
        "length_unit": "Python character offsets; not tokenizer tokens",
        "aggregation": "mean within each paired fact group, then equal mean across 23 groups",
        "iou_rank_is_display_only": True,
        "frontier_objectives": ["evidence_char_precision_macro", "evidence_char_recall_macro"],
        "gold_span_semantic_review_completed": False,
        "new_model_calls": 0, "holdout_questions_loaded": False,
        "original_results_modified": False,
        "input_sha256": hashes, "script_sha256": sha(Path(__file__)),
        "inherited_mapping_inputs_checked": len(provenance["input_sha256"]),
        "all_original_precision_recall_summaries_matched": True,
        "method_reference": "https://github.com/brandonstarxel/chunking_evaluation/blob/e708410d1c61cb76a85cd9d433630ef89b9c6b85/chunking_evaluation/evaluation_framework/base_evaluation.py",
    })

    lines = [
        "# 18个分块配置：证据字符 P/R 与 IoU 比较", "",
        "日期：2026-10-13。已有调参结果的离线补充分析；不调用模型、不读取保留题，原始结果保持可追溯。", "",
        "## 范围与口径", "",
        "18个配置、46个问法、23个事实组、两个视图，共1,656条逐题指标和36组汇总。主要视图为共同2560本地token预算，top-5单独报告。", "",
        "设 G 为去重标注证据字符数，C 为召回覆盖的去重证据字符数，R 为召回块原文字符数之和（包括块间重复）。证据字符 Precision=C/R，Recall=C/G；本次追加 IoU=C/(R+G-C)。", "",
        "IoU同时反映未覆盖证据与多带回/重复的文字。它不能消除标注错误，也不证明回答正确。每题先计算指标，组内两问法平均后再对23组平均；不能从两个宏平均P/R直接算出宏平均IoU。", "",
        "表格按IoU均值排序便于阅读，不据此自动选定最终配置。字符P/R前沿表示没有其他配置在这两项均值上同时不低、且至少一项更高；它不是显著性检验或业务验收门槛。", "",
    ]
    for view, title in ((VIEWS[0], "固定2560-token预算"), (VIEWS[1], "固定top-5")):
        subset = sorted((row for row in summaries if row["view"] == view),
                        key=lambda row: (-row["evidence_char_iou_macro"], row["candidate_id"]))
        lines += [f"## {title}", "",
                  "| 配置 | 证据字符P | 证据字符R | 证据字符IoU | 块P | 块R | 平均召回块数 | 字符P/R前沿 |",
                  "|---|---:|---:|---:|---:|---:|---:|---|"]
        for row in subset:
            lines.append(
                f"| {row['candidate_id']} | {row['evidence_char_precision_macro']:.2%} | "
                f"{row['evidence_char_recall_macro']:.2%} | {row['evidence_char_iou_macro']:.2%} | "
                f"{row['chunk_precision_overlap_macro']:.2%} | {row['chunk_recall_overlap_macro']:.2%} | "
                f"{row['mean_retrieved_chunks']:.2f} | {'是' if row['on_character_pr_frontier'] else '否'} |"
            )
        lines += ["", "字符P/R前沿：" + "、".join(row["candidate_id"] for row in subset
                                                    if row["on_character_pr_frontier"]) + "。", ""]
    lines += [
        "## 结果边界与后续判读", "",
        "- gold沿用现有来源区间；29个独立区间中28个为整条article。有些问题只需要条文的一部分，其他有效证据也未穷尽。上述数值只反映这些标注区间的覆盖与额外内容。",
        "- 块相关性为任意字符交集；块级Recall分母随分块方案变化。因此跨切法优先比较同一gold区间的字符指标，并检查具体案例。",
        "- 这里计算的是字符IoU。Chroma报告称token级，但所参考的源码按字符区间求长度；本项目显式对gold区间去重，与源码直接相加的实现边界有区别。",
        "- 没有追加Precision Ω；该仓库的实现取所有证据相交块的区间并集，与实际检索Precision的重复计数口径不同，不能直接当作任意检索策略的严格上界。",
        "- 形成检索候选后，应检查候选逐题证据遗漏及较宽gold区间的必要性。实际回答检查、已知事实F1异常校准以及可选AnswerRelevancy属于后续整体RAG验证；不作为本次离线指标补算的前置。",
        "- 当前没有最终选定分块配置，也没有运行保留题。选定主配置和验证规则后，再对7个保留事实组作初步确认。", "",
        "## 产物与重现", "",
        "`metrics.csv` / `metrics.json`：全部18配置×两视图汇总；`per_query.json`：逐题计数与IoU；`provenance.json`：公式、范围、哈希及原P/R一致性。", "",
        "```bash", "pilot/.venv-rag-eval/bin/python pilot/rag_pipeline/compare_evidence_retrieval.py", "```", "",
        "参考：[Chroma检索分块评测源码](https://github.com/brandonstarxel/chunking_evaluation/blob/e708410d1c61cb76a85cd9d433630ef89b9c6b85/chunking_evaluation/evaluation_framework/base_evaluation.py)。", "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("output:", out)
    print("rows:", len(per_query), "summaries:", len(summaries))
    for view in VIEWS:
        print(view)
        for row in sorted((row for row in summaries if row["view"] == view),
                          key=lambda row: -row["evidence_char_iou_macro"])[:4]:
            print(row["candidate_id"],
                  f"P={row['evidence_char_precision_macro']:.2%}",
                  f"R={row['evidence_char_recall_macro']:.2%}",
                  f"IoU={row['evidence_char_iou_macro']:.2%}",
                  "frontier=" + str(row["on_character_pr_frontier"]))


if __name__ == "__main__":
    main()
