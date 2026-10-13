#!/usr/bin/env python3
"""Compare frozen judge prompts on frozen controlled answers, using real Ragas.

This is a model evaluation experiment requested by the user, not a unit-test suite.
Agent control labels are never presented as human gold or fed to judge prompts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from run_model_eval import (Gateway, JsonRagasLLM, JOB, ROOT, json_safe, save_json,
                            stable_json, usage_summary)
from calibrated_metrics import (ANSWER_ADEQUACY_PROMPT, ANSWER_ADEQUACY_PROMPT_V2, BASELINE_CRITERION,
                                PRESERVATION_RULES, NLI_RULES, answer_adequacy,
                                diagnostic_metrics)


async def run(config_path: Path) -> None:
    config = json.loads(config_path.read_text())
    output = ROOT / config["execution"]["output_root"] / config["run_id"]
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / config["calibration_file"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != config["calibration_sha256"]:
        raise ValueError("Calibration input hash changed")
    snapshot = output / "config.resolved.json"
    if snapshot.exists() and json.loads(snapshot.read_text())["config"] != config:
        raise ValueError("Use a new run ID for changed calibration settings")
    save_json(snapshot, {"config": config, "code_sha256": {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__), Path(__file__).with_name("calibrated_metrics.py"), Path(__file__).with_name("run_model_eval.py")]},
                         "prompts": {"baseline": BASELINE_CRITERION, "criteria_v1": ANSWER_ADEQUACY_PROMPT, "criteria_v2": ANSWER_ADEQUACY_PROMPT_V2,
                                     "preservation_rules_v1": PRESERVATION_RULES, "nli_rules_v1": NLI_RULES}})
    rows = [json.loads(l) for l in source.read_text().splitlines()]
    profiles = config.get("calibration_profiles", ["baseline", "criteria_v1"])
    gateway = Gateway(config, output)
    llm = JsonRagasLLM(gateway)
    results = []
    started = time.perf_counter()
    try:
        for row in rows:
            record = {**row, "judge_labels": {}, "diagnostic_metrics": {}}
            for profile in profiles:
                JOB.set(f"calibration:{row['case_id']}:{profile}:AnswerAdequacy")
                try:
                    metric = answer_adequacy(baseline=profile == "baseline", version="criteria_v1" if profile == "baseline" else profile)
                    value = await metric.ascore(llm=llm, question=row["question"], response=row["response"],
                                                grading_notes=row["grading_notes"], source_context=row["source_context"])
                    record["judge_labels"][profile] = {"status": "ok", "value": value.value, "reason": value.reason,
                                                       "agrees_with_agent_control": value.value == row["expected_label"]}
                except Exception as error:
                    record["judge_labels"][profile] = {"status": "error", "value": None, "error": gateway.safe_error(error)}
                print(stable_json({"case_id": row["case_id"], "profile": profile, **record["judge_labels"][profile]}), flush=True)
            if row["detailed_ragas_comparison"]:
                for profile in ("ragas_default", "preserved_v1"):
                    record["diagnostic_metrics"][profile] = {}
                    for name, metric in diagnostic_metrics(llm, preserved=profile == "preserved_v1").items():
                        JOB.set(f"calibration:{row['case_id']}:{profile}:{name}")
                        try:
                            params = {"response": row["response"], "reference": row["reference"]} if name == "FactualCorrectness" else {"user_input": row["question"], "response": row["response"], "retrieved_contexts": [row["source_context"]]}
                            value = await metric.ascore(**params)
                            record["diagnostic_metrics"][profile][name] = {"status": "ok", "value": float(value.value), "details": json_safe(vars(value))}
                        except Exception as error:
                            record["diagnostic_metrics"][profile][name] = {"status": "error", "value": None, "error": gateway.safe_error(error)}
                        print(stable_json({"case_id": row["case_id"], "profile": profile, "metric": name,
                                           **record["diagnostic_metrics"][profile][name]}), flush=True)
            results.append(record)
            save_json(output / "results.json", results)
        agreement = {}
        for profile in profiles:
            scored = [r for r in results if r["judge_labels"][profile]["status"] == "ok"]
            mismatched = [r["case_id"] for r in scored if not r["judge_labels"][profile]["agrees_with_agent_control"]]
            agreement[profile] = {"valid": len(scored), "matched": len(scored)-len(mismatched), "mismatched_case_ids": mismatched,
                                  "agreement_with_agent_controls": (len(scored)-len(mismatched))/len(scored) if scored else None}
        summary = {"status": "diagnostic_completed_pending_human_review", "cases": len(results), "agreement": agreement,
                   "elapsed_seconds": round(time.perf_counter()-started, 2), "usage": usage_summary(gateway),
                   "limits": ["Agent labels, not independent human gold", "Small constructed controls, not business reliability or chunking effects",
                              "Additional checks are not a blind holdout", "Factual F1 can penalize valid extra facts absent from a short reference by design",
                              "Prompt variants frozen before this calibration run; no metric formulas changed"]}
        save_json(output / "summary.json", summary)
        lines = ["# " + config.get("report_title", "Judge 校准诊断 v1"), "", "固定控制回答和代理预期标签，不生成新的 RAG 回答。人工审核待完成；一致率不是人类专家一致率或分块效果。", "",
                 "| 提示词 | 成功题数 | 与代理控制一致 | 不一致案例 |", "|---|---:|---:|---|"]
        for profile, stats in agreement.items():
            lines.append(f"| {profile} | {stats['valid']} | {stats['matched']} | {', '.join(stats['mismatched_case_ids']) or '无'} |")
        lines.extend(["", "## 每个控制案例", ""])
        for row in results:
            lines.extend([f"### {row['case_id']} · {row['qa_id']}", "", f"代理预期：{row['expected_label']}（待人工确认）", "",
                          f"问题：{row['question']}", "", row["response"], "", "```json", json.dumps(row["judge_labels"], ensure_ascii=False, indent=2), "```", ""])
            if row["diagnostic_metrics"]:
                lines.extend(["Ragas 指标（公式未修改）：", "", "```json", json.dumps({p:{k:v["value"] for k,v in m.items()} for p,m in row["diagnostic_metrics"].items()}, ensure_ascii=False, indent=2), "```", ""])
        lines.extend([f"标准价用量估算：{summary['usage']['estimated_standard_cost_cny']} 元，非实际账单。", "",
                      "实现依据：[Ragas judge alignment](https://docs.ragas.io/en/stable/howtos/applications/align-llm-as-judge/)、[官方提示词定制](https://docs.ragas.io/en/stable/howtos/customizations/metrics/modifying-prompts-metrics/)。"])
        (output / "report.md").write_text("\n".join(lines))
        print(stable_json({"phase": "complete", "agreement": agreement, "usage": summary["usage"]}), flush=True)
    finally:
        await gateway.client.aclose()
        save_json(output / "attempt_usage.json", usage_summary(gateway))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
