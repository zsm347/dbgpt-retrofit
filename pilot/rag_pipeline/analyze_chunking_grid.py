#!/usr/bin/env python3
"""Describe the frozen M4 grid with paired question results and runtime costs.

This is exploratory analysis, not independent validation or human certification.
Partial grids are explicitly marked, and incomplete candidates get no pass rate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWS = ("fixed_local_token_budget", "fixed_top_k")


def load(path: Path):
    return json.loads(path.read_text())


def save(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mean(values):
    return round(statistics.mean(values), 4) if values else None


def summarize(rows: list[dict], qa: dict, rates: dict) -> dict:
    expected = set(qa)
    by_id = {r["qa_id"]: r for r in rows}
    labels = {qid: r.get("metrics", {}).get("AnswerAdequacy", {}).get("value")
              for qid, r in by_id.items()
              if r.get("metrics", {}).get("AnswerAdequacy", {}).get("status") == "ok"}
    complete = len(rows) == len(qa) and set(by_id) == expected and set(labels) == expected
    details = {}
    for field in ("question_language", "question_type", "doc_path"):
        values = sorted({q[field] for q in qa.values()})
        details[field] = {}
        for value in values:
            ids = {qid for qid, q in qa.items() if q[field] == value}
            details[field][value] = {
                "expected": len(ids), "scored": len(ids & set(labels)),
                "passed": sum(labels.get(qid) == 1 for qid in ids),
            }
    costs, prompt_tokens, output_tokens = [], [], []
    for r in rows:
        usage = r.get("answer_usage", {})
        if "prompt_tokens" in usage and "completion_tokens" in usage:
            inp, out = usage["prompt_tokens"], usage["completion_tokens"]
            prompt_tokens.append(inp)
            output_tokens.append(out)
            costs.append((inp * rates["llm_input_per_million"] +
                          out * rates["llm_output_per_million"]) / 1e6)
    diagnostic = {}
    for name in ("ContextRecall", "ContextPrecision", "FactualCorrectness", "Faithfulness"):
        valid = [r["metrics"][name]["value"] for r in rows
                 if r.get("metrics", {}).get(name, {}).get("status") == "ok"]
        diagnostic[name] = {"scored": len(valid), "mean": mean(valid) if len(valid) == len(qa) else None}
    # The auxiliary source-span coverage is not a substitute for answer grading.
    # Keep it as a separate retrieval sanity check because a judge can mark a
    # refusal as adequate when the answerable source span was never retrieved.
    coverage_miss = [r["qa_id"] for r in rows if r.get("evidence_char_coverage_aux", 0) <= 0]
    auto_pass_without_coverage = [
        r["qa_id"] for r in rows
        if r.get("metrics", {}).get("AnswerAdequacy", {}).get("status") == "ok"
        and r.get("metrics", {}).get("AnswerAdequacy", {}).get("value") == 1
        and r.get("evidence_char_coverage_aux", 0) <= 0
    ]
    coverage_gated_values = [
        1 if r.get("metrics", {}).get("AnswerAdequacy", {}).get("value") == 1
        and r.get("evidence_char_coverage_aux", 0) > 0 else 0
        for r in rows
        if r.get("metrics", {}).get("AnswerAdequacy", {}).get("status") == "ok"
    ]
    return {
        "complete": complete, "questions": len(qa), "scored": len(labels),
        "passed": sum(v == 1 for v in labels.values()),
        "pass_rate": mean(list(labels.values())) if complete else None,
        "pass_qa_ids": [qid for qid in qa if labels.get(qid) == 1],
        "fail_qa_ids": [qid for qid in qa if labels.get(qid) == 0],
        "mean_context_local_tokens": mean([r["context_local_tokens"] for r in rows]),
        "mean_answer_provider_input_tokens": mean(prompt_tokens),
        "mean_answer_provider_output_tokens": mean(output_tokens),
        "answer_usage_count": len(costs),
        "mean_answer_standard_cost_cny": round(statistics.mean(costs), 8) if len(costs) == len(qa) else None,
        "diagnostic_metrics": diagnostic, "strata": details,
        "mean_aux_char_coverage": mean([r["evidence_char_coverage_aux"] for r in rows]),
        "retrieval_sanity": {
            "coverage_miss_count": len(coverage_miss),
            "coverage_miss_qa_ids": coverage_miss,
            "auto_pass_without_coverage_count": len(auto_pass_without_coverage),
            "auto_pass_without_coverage_qa_ids": auto_pass_without_coverage,
            "coverage_gated_passed": sum(coverage_gated_values),
            "coverage_gated_pass_rate": mean(coverage_gated_values) if len(coverage_gated_values) == len(qa) else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "pilot/experiments/chunking/m4_grid_manifest_v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "pilot/experiments/chunking/m4_grid_analysis_v1")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = load(args.manifest)
    qa_path = ROOT / "pilot/data/eval/qa_text_tuning_v3.jsonl"
    if file_hash(qa_path) != manifest["qa_sha256"]:
        raise RuntimeError("Frozen QA changed; do not combine versions")
    qa = {q["qa_id"]: q for q in (json.loads(line) for line in qa_path.read_text().splitlines() if line.strip())}
    candidate_file = ROOT / "pilot/experiments/chunking/m4_candidates_v1.json"
    if file_hash(candidate_file) != manifest["candidate_file_sha256"]:
        raise RuntimeError("Frozen candidate definitions changed")
    candidate_def = {c["candidate_id"]: c for c in load(candidate_file)["candidates"]}
    candidates, missing, inputs = [], [], {str(qa_path.relative_to(ROOT)): file_hash(qa_path)}
    row_sets = {}
    failure_rows = []
    for run in manifest["runs"]:
        config_path = ROOT / run["config_path"]
        if file_hash(config_path) != run["config_file_sha256"]:
            raise RuntimeError("Frozen candidate config changed")
        config = load(config_path)
        directory = ROOT / config["execution"]["output_root"] / run["run_id"]
        if not all((directory / name).exists() for name in ("summary.json", "results.json")):
            missing.append(run["candidate_id"])
            continue
        summary, rows = load(directory / "summary.json"), load(directory / "results.json")
        for name in ("summary.json", "results.json", "config.resolved.json"):
            path = directory / name
            inputs[str(path.relative_to(ROOT))] = file_hash(path)
        resolved = load(directory / "config.resolved.json")
        if resolved["qa_sha256"] != manifest["qa_sha256"]:
            raise RuntimeError("Candidate result uses a different QA version")
        definition = candidate_def[run["candidate_id"]]
        entry = {"candidate_id": run["candidate_id"], "declared_aliases": run["declared_aliases"],
                 "strategy": definition["strategy"], "chunk_size": definition["chunk_size"],
                 "overlap": definition["overlap"], "chunk_count": definition["chunk_count"],
                 "index_local_tokens": definition["index_local_tokens"],
                 "result_directory": str(directory.relative_to(ROOT)), "error_count": summary["error_count"],
                 "views": {view: summarize([r for r in rows if r["view"] == view], qa, config["cost_estimate"])
                           for view in VIEWS}}
        candidates.append(entry)
        row_sets[run["candidate_id"]] = {(r["qa_id"], r["view"]): r for r in rows}
        for r in rows:
            metric = r.get("metrics", {}).get("AnswerAdequacy", {})
            if metric.get("status") != "ok" or metric.get("value") != 1:
                failure_rows.append({"candidate_id": run["candidate_id"], "qa_id": r["qa_id"],
                                     "view": r["view"], "question": r["user_input"],
                                     "reference": r["reference"], "response": r["response"],
                                     "criterion": metric, "context_local_tokens": r["context_local_tokens"],
                                     "aux_char_coverage": r["evidence_char_coverage_aux"],
                                     "detail_path": str((directory / (r["result_id"].replace(":", "_") + ".md")).relative_to(ROOT))})
    complete = len(candidates) == manifest["distinct_runs"] and all(
        c["error_count"] == 0 and all(v["complete"] for v in c["views"].values()) for c in candidates)
    eligible = [c for c in candidates if c["error_count"] == 0 and c["views"][VIEWS[0]]["complete"]
                and c["views"][VIEWS[0]]["mean_answer_standard_cost_cny"] is not None]
    # Conservative protocol: keep equal-quality candidates for paired error and cost review.
    for c in candidates:
        primary = c["views"][VIEWS[0]]
        c["strictly_dominated_by"] = [d["candidate_id"] for d in eligible
            if primary["pass_rate"] is not None and primary["mean_answer_standard_cost_cny"] is not None
            and d["views"][VIEWS[0]]["passed"] > primary["passed"]
            and d["views"][VIEWS[0]]["mean_answer_standard_cost_cny"] < primary["mean_answer_standard_cost_cny"]]
    order = sorted(eligible, key=lambda c: (-c["views"][VIEWS[0]]["passed"],
                                           c["views"][VIEWS[0]]["mean_answer_standard_cost_cny"], c["candidate_id"]))
    pairings = []
    for i, left in enumerate(eligible):
        for right in eligible[i+1:]:
            la = set(left["views"][VIEWS[0]]["pass_qa_ids"])
            ra = set(right["views"][VIEWS[0]]["pass_qa_ids"])
            pairings.append({"left": left["candidate_id"], "right": right["candidate_id"],
                             "both_pass": len(la & ra), "both_fail": len(set(qa) - la - ra),
                             "only_left_pass": sorted(la - ra), "only_right_pass": sorted(ra - la)})
    matrix = []
    for qid, question in qa.items():
        judgments = {}
        for cid, rows in row_sets.items():
            r = rows.get((qid, VIEWS[0]), {})
            judgments[cid] = r.get("metrics", {}).get("AnswerAdequacy", {}).get("label")
        matrix.append({"qa_id": qid, "question": question["question"],
                       "language": question["question_language"], "source": question["doc_path"],
                       "candidate_budget_labels": judgments, "label_counts": dict(Counter(judgments.values()))})
    result = {"status": "complete_grid_exploratory_pending_review" if complete else "partial_grid_no_final_comparison",
              "review_status": "agent_source_reviewed_pending_human_review",
              "declared_candidates": manifest["declared_candidate_count"], "distinct_runs_expected": manifest["distinct_runs"],
              "completed_result_sets": len(candidates), "missing": missing,
              "primary_view": VIEWS[0], "candidates": candidates,
              "quality_then_answer_cost_order": [c["candidate_id"] for c in order],
              "strict_quality_and_answer_cost_frontier": [c["candidate_id"] for c in eligible if not c["strictly_dominated_by"]],
              "paired_budget_comparisons": pairings, "budget_question_matrix": matrix,
              "input_sha256": inputs,
              "limitations": ["Agent QA and LLM judge; independent human review pending",
                              "24 tuning questions, related sources; no blind validation or significance claim",
                              "Answer cost excludes judge and amortized indexing; provider usage not local tokens",
                              "Cached requests are replayed, not independent repetitions",
                              "All views use the same 24 questions; no doubled sample size",
                              "Strict domination requires better quality and lower answer cost; equal-quality cases remain for review"]}
    save(args.output / "analysis.json", result)
    save(args.output / "failures.json", failure_rows)
    lines = ["# M4 粗搜参数比较", "", f"状态：{result['status']}。独立人工审核待完成；本报告不是最终盲测结论。", "",
             f"18 个配置身份、16 组独立块集合；已读取 {len(candidates)}/16 组结果。主要视角为 2560 本地 token 预算上限。", "",
             "初始回源检查已经发现项目按题 judge 对真实回答存在关系/归因漏判。以下是原始自动评分，不是人工正确率；排名必须结合逐题复核，不能直接宣布最优配置。", "",
             "| 配置 | 预算自动通过/24 | top-k 自动通过/24 | 块数 | 预算平均本地 tokens | 回答平均 provider 输入 tokens | 每千次回答标准价估算（元） |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for c in order:
        b, k = (c["views"][v] for v in VIEWS)
        lines.append(f"| {c['candidate_id']} | {b['passed']} | {k['passed']} | {c['chunk_count']} | {b['mean_context_local_tokens']} | {b['mean_answer_provider_input_tokens']} | {b['mean_answer_standard_cost_cny'] * 1000:.4f} |")
    lines.extend(["", "以上成本只计回答模型，按请求返回的 provider 用量和配置中的标准价估算；不含 judge、索引摊销、折扣或免费额度。缓存回复的原始用量用于描述同一请求的理论成本，缓存节省不算线上性能收益。", "",
                  "检索 sanity check（不改写自动评分）：evidence_char_coverage_aux=0 表示已知 gold 原文区间未被召回。部分拒答仍会被自动 AnswerAdequacy 判为通过，因此必须同时查看 analysis.json 中的 retrieval_sanity。", "",
                  "| 配置 | 预算 coverage-gated/24 | 预算无覆盖却自动通过 | top-k coverage-gated/24 | top-k 无覆盖却自动通过 |", "|---|---:|---:|---:|---:|"])
    for c in order:
        b, k = (c["views"][v] for v in VIEWS)
        br, kr = b["retrieval_sanity"], k["retrieval_sanity"]
        lines.append(f"| {c['candidate_id']} | {br['coverage_gated_passed']} | {br['auto_pass_without_coverage_count']} | {kr['coverage_gated_passed']} | {kr['auto_pass_without_coverage_count']} |")
    lines.extend(["", "coverage-gated 只用于检索 sanity check：它要求自动通过且 gold 原文区间有覆盖，不替换 Ragas 原始标签，也不能替代人工检查。", "",
                  "同题配对、语言/题型/文档分层、严格质量与成本支配关系见 analysis.json；所有按题失败和原判断见 failures.json。不能把并列均分、差一题约 4.17 个百分点或一次 LLM 判断差异解释为统计显著。", "",
                  "C 1024 的三种重叠完全等价，结果复用但配置身份保留；不推论其他大小的 overlap 无效。", ""])
    if not complete:
        lines.extend(["未完成全部成功结果，不能做最终候选比较。缺失：" + ", ".join(missing), ""])
    (args.output / "report.md").write_text("\n".join(lines))
    print(json.dumps({"status": result["status"], "completed": len(candidates), "failure_rows": len(failure_rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
