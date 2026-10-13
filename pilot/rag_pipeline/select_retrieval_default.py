#!/usr/bin/env python3
"""Record a tuning-stage retrieval default using existing grid results only."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
GRID = ROOT / "pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3"
SELECTED = "B_recursive_s256_o0"
VIEWS = ("fixed_local_token_budget", "fixed_top_k")
PAIRED_OTHERS = (
    "B_recursive_s256_o25", "C_structure_s256_o51",
    "C_structure_s512_o0", "B_recursive_s512_o51",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    paths = [GRID / name for name in (
        "retrieval_selection_v1/metrics.json", "retrieval_selection_v1/per_query.json",
        "retrieval_selection_v1/provenance.json", "analysis_v1/metrics.json",
        "analysis_v1/integrity.json", "grid_manifest.json", "configs/s256_o0.json",
    )]
    hashes = {str(p.relative_to(ROOT)): digest(p) for p in paths}
    retrieval, per_query, provenance, answer, integrity, manifest, config = (
        json.loads(p.read_text(encoding="utf-8")) for p in paths
    )
    if provenance["holdout_questions_loaded"] or integrity["holdout_query_file_loaded"]:
        raise ValueError("Expected tuning-only analysis")
    for relative, expected in provenance["input_sha256"].items():
        if digest(ROOT / relative) != expected:
            raise ValueError(f"Evidence-mapping input changed: {relative}")
    answer_lookup = {(r["candidate_id"], r["view"]): r for r in answer["entries"]
                     if r["stratum"] == "all"}
    definitions = {r["candidate_id"]: r for r in manifest["candidate_configs"]}
    expected_keys = {(cid, view) for cid in definitions for view in VIEWS}
    if len(definitions) != 18 or set(answer_lookup) != expected_keys:
        raise ValueError("Expected all 18 configurations and both answer views")
    if {(r["candidate_id"], r["view"]) for r in retrieval} != expected_keys:
        raise ValueError("Missing retrieval configuration/view")
    joined = []
    for r in retrieval:
        a = answer_lookup[r["candidate_id"], r["view"]]
        if r["query_count"] != 46 or r["fact_group_count"] != 23:
            raise ValueError("Unexpected evaluation scope")
        d = definitions[r["candidate_id"]]
        joined.append({
            **r, "strategy": d["strategy"], "chunk_size_local_tokens": d["chunk_size"],
            "overlap_local_tokens": d["overlap"],
            **{f"ragas_{k}_raw": v["mean"] for k, v in a["metrics"].items()},
            **a["resources"],
        })
    lookup = {(r["candidate_id"], r["view"]): r for r in joined}
    groups = defaultdict(list)
    for r in per_query:
        if r["view"] == VIEWS[0]:
            groups[r["candidate_id"], r["pair_group_id"]].append(r)
    group_ids = sorted({key[1] for key in groups})
    if len(group_ids) != 23 or any(len(rows) != 2 for rows in groups.values()):
        raise ValueError("Expected 23 paired fact groups")
    indices = np.random.default_rng(20261036).integers(0, 23, size=(10000, 23))
    comparisons = []
    for other in PAIRED_OTHERS:
        for metric in ("evidence_char_precision", "evidence_char_recall", "evidence_char_iou"):
            delta = np.array([
                mean(r[metric] for r in groups[SELECTED, gid])
                - mean(r[metric] for r in groups[other, gid]) for gid in group_ids
            ])
            low, high = np.quantile(delta[indices].mean(axis=1), [0.025, 0.975])
            comparisons.append({
                "selected": SELECTED, "other": other, "view": VIEWS[0], "metric": metric,
                "mean_difference_pp": float(delta.mean() * 100),
                "exploratory_ci95_pp": [float(low * 100), float(high * 100)],
                "higher_fact_groups": int((delta > 1e-12).sum()),
                "same_fact_groups": int((np.abs(delta) <= 1e-12).sum()),
                "lower_fact_groups": int((delta < -1e-12).sum()),
            })
    controls = {
        "status": "tuning_selection_controls; not a holdout runner configuration",
        "candidate_id": SELECTED, "source_grid_config": definitions[SELECTED]["config_path"],
        "corpus_manifest": config["corpus_manifest"], "length": config["length"],
        "chunking": {**config["chunking"], "strategies": ["B_recursive"]},
        "embedding": config["embedding"], "retrieval": config["retrieval"],
        "context": config["context"], "answer": config["answer"], "judge": config["judge"],
        "comparison_primary_view": VIEWS[0], "comparison_secondary_view": VIEWS[1],
        "gold_policy": "Existing reviewed source spans unchanged; no post-result relabeling",
        "answer_score_policy": "Original Ragas scores are auxiliary; uncalibrated factual F1 does not decide selection",
    }
    for relative, expected in hashes.items():
        if digest(ROOT / relative) != expected:
            raise ValueError(f"Input changed during analysis: {relative}")
    out = GRID / "selection_decision_v1"
    out.mkdir(exist_ok=True)
    joined.sort(key=lambda r: (VIEWS.index(r["view"]), -r["evidence_char_iou_macro"], r["candidate_id"]))
    save(out / "metrics.json", joined)
    with (out / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(joined[0]))
        w.writeheader()
        w.writerows(joined)
    save(out / "paired_comparisons.json", comparisons)
    save(out / "selected_controls.json", controls)
    save(out / "decision.json", {
        "status": "tuning_retrieval_default_selected_pending_holdout_confirmation",
        "selected_candidate": SELECTED, "scope": "Retrieval default for this frozen corpus and evaluation labeling",
        "selection_rule_timing": "posthoc; recorded after examining the grid, not preregistered",
        "selection_basis": [
            "Joint character P/R/IoU under the fixed 2560-local-token budget",
            "B/256/25 has slightly higher budget means; no claim that B/256/0 is statistically superior or equivalent",
            "Zero overlap is preferred for a simpler default when observed mean differences are very small; no formal noninferiority margin",
            "B/256/0 has highest fixed-top-5 character IoU and lower index volume than B/256/25",
            "Raw answer metrics provide auxiliary context, not a combined score or calibrated factual-quality verdict",
        ],
        "not_selected_as_jointly_optimal": True, "known_factual_f1_anomaly_resolved": False,
        "gold_spans_changed": False, "all_missed_queries_reaudited": False,
        "new_model_calls": 0, "holdout_questions_loaded": False,
        "next_step": "Confirm this fixed default on the seven held-out fact groups; no new 18-config search on holdout",
        "acceptance_threshold": None,
        "holdout_interpretation": "Small descriptive confirmation; no preset universal pass threshold; indexed source documents are already seen",
    })
    save(out / "provenance.json", {
        "input_sha256": hashes, "script_sha256": digest(Path(__file__)),
        "candidate_count": 18, "summary_count": len(joined), "primary_fact_groups": 23,
        "new_model_calls": 0, "holdout_questions_loaded": False,
        "original_results_modified": False,
        "bootstrap": {"seed": 20261036, "repetitions": 10000, "unit": "paired fact group",
                      "multiple_comparison_adjustment": False,
                      "scope": "Exploratory post-selection uncertainty; not an equivalence test"},
    })
    lines = [
        "# 调参阶段分块选择：B 递归，256本地token，重叠0", "",
        "日期：2026-10-13。复用全部18配置、两视图的检索指标、原始Ragas指标及资源记录；不调用模型、不读取保留题、不修订gold。", "",
        "## 选择结论和依据", "",
        "将B_recursive_s256_o0选为当前冻结知识库的检索默认配置，待7个保留事实组/14问法初步确认。它不是各项指标第一，也不是已证明的全流程最优。选择规则是观察结果后的工程取舍，未预注册。", "",
        "主视图仍为2560本地token预算。B/256/25的字符P/R/IoU均值最高或接近最高；B/256/0比它分别低0.0216、0.0676、0.0232个百分点。没有正式非劣效界值，不能把小差异解释为统计等价。选择无重叠作为更简单的默认，并参考它在top-5的较高IoU；不因仅少16块就宣称有显著费用或延迟优势。", "",
        "B/256/0在预算视图的字符P/R/IoU为10.77%/95.87%/10.68%，在top-5为20.40%/90.39%/19.80%。C/256/51的预算字符P略高，但字符R低3.11个百分点；C/512/0原始回答事实F1较高，但预算字符R为88.22%。因此不能称C全面更差，也不能称B全面占优。", "",
        "1024块在top-5可覆盖更多证据，同时带入约4300—5000本地tokens，超过主视图2560预算；它们的top-5高Recall不能直接裁定共同预算下的胜者。512块的索引向量条数较少，属于真实资源取舍，但本次默认优先保留证据覆盖与内容密度。", "",
        "## 全部配置的证据检索结果", "",
    ]
    for view, title in zip(VIEWS, ("固定2560本地token预算", "固定top-5")):
        lines += [f"### {title}", "", "| 配置 | 字符P | 字符R | 字符IoU | 块P | 块R | 平均上下文本地tokens |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for r in joined:
            if r["view"] == view:
                fields = ("evidence_char_precision_macro", "evidence_char_recall_macro", "evidence_char_iou_macro",
                          "chunk_precision_overlap_macro", "chunk_recall_overlap_macro")
                lines.append("| " + r["candidate_id"] + " | " + " | ".join(f"{r[k]:.2%}" for k in fields)
                             + f" | {r['context_local_tokens_mean']:.1f} |")
        lines.append("")
    lines += ["## 已有回答指标及索引量：辅助判断", "",
              "以下为主预算视图。完整18配置、两视图的四项Ragas和资源字段见metrics.csv/json。表格展示原始分数；事实F1引用惩罚未校准，不据此确定正确率或最终问答优胜。", "",
              "| 配置 | ContextRecall | ContextPrecision | 原始事实F1 | Faithfulness | 块数 | 索引本地tokens |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for cid in (SELECTED, *PAIRED_OTHERS, "B_recursive_s256_o51"):
        r = lookup[cid, VIEWS[0]]
        fields = ("ragas_ContextRecall_raw", "ragas_ContextPrecision_raw", "ragas_FactualCorrectness_raw", "ragas_Faithfulness_raw")
        lines.append("| " + cid + " | " + " | ".join(f"{r[k]:.2%}" for k in fields)
                     + f" | {r['chunk_count']} | {r['index_local_tokens']} |")
    lines += ["", "索引本地token是cl100k_base计数，不是Embedding账单；向量条数不等于磁盘大小。B/256/0索引本地tokens为283518，B/256/25为286550，差约1.1%。所有候选的预算上限相同，实际provider输入/输出用量仍有差异；本次没有价格或线上延迟对照，不宣称哪个实际费用最低。", "",
              "## 同事实组差异：探索性范围", "",
              "方向为选定B/256/0减对照配置。两问法先平均，再对23事实组bootstrap，10000次；未校正多重比较，没有建模judge或相关法律主题的额外波动。区间包含0不证明两配置等价。", "",
              "| 对照 | 指标 | 差异（百分点） | 探索性95%区间 | B较高/相同/较低组数 |",
              "|---|---|---:|---|---|"]
    for r in comparisons:
        lo, hi = r["exploratory_ci95_pp"]
        lines.append(f"| {r['other']} | {r['metric']} | {r['mean_difference_pp']:+.4f} | [{lo:+.4f}, {hi:+.4f}] | {r['higher_fact_groups']}/{r['same_fact_groups']}/{r['lower_fact_groups']} |")
    lines += ["", "## 使用边界与后续", "",
              "本次沿用原证据位置标签，包含较宽条文和未穷尽替代证据的边界。没有发现具体标注错误就不因排名改gold；漏召回逐题复盘是可选解释分析，不再是选型前置。", "",
              "已记录一个调参阶段默认。下一步为固定设置后的保留题确认：只评价这个选择，不把7个保留事实组重新当作18配置搜索集；若根据它调参，应标记已参与开发。保留来源文档已入索引，因此不是未见文档泛化测试。无预先认可的上线阈值，本轮以逐指标和不确定性作描述性确认，不事后补一个通过门槛。", "",
              "AnswerRelevancy和事实F1校准仍可用于后续完整问答质量评估；本次没有执行它们，也不让它们阻塞检索默认选型。尚未修改线上DB-GPT的知识库配置。", "",
              "## 产物与重现", "",
              "decision.json记录选择和边界；selected_controls.json保存选定策略的控制条件（不是可直接运行的holdout配置）；metrics.csv/json保存18配置两视图的检索、原始Ragas及资源字段；paired_comparisons.json保存同事实组差异；provenance.json保存输入和脚本哈希。", "",
              "```bash", "pilot/.venv-rag-eval/bin/python pilot/rag_pipeline/select_retrieval_default.py", "```", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"selected": SELECTED, "summary_rows": len(joined), "new_model_calls": 0,
                      "holdout_questions_loaded": False, "report": str(out / "report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
