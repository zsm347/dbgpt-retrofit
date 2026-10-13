#!/usr/bin/env python3
"""Audit and describe a completed paired-query grid without new model calls.

Four Ragas metrics stay separate. Query variants are clustered by fact group;
bootstrap intervals and candidate recommendations are explicitly exploratory.
Only the frozen tuning file and its experiment corpus are loaded.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import tiktoken

ROOT = Path(__file__).resolve().parents[2]
METRICS = ("ContextRecall", "ContextPrecision", "FactualCorrectness", "Faithfulness")
VIEWS = ("fixed_local_token_budget", "fixed_top_k")
EPS = 1e-9
SEED = 20261013


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(stable(value).encode()).hexdigest()


def resolve(path):
    path = Path(path).expanduser()
    return path if path.is_absolute() else ROOT / path


def relative(path):
    return str(Path(path).resolve().relative_to(ROOT))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_csv(path, rows):
    if not rows:
        return
    with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def mean(values):
    return float(np.mean(values))


def intervals(ranges):
    merged = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def coverage(question, chunks):
    gold = intervals((s["start_char"], s["end_char"]) for s in question["source_spans"])
    found = intervals((c["source_start"], c["source_end"]) for c in chunks
                      if c["doc_id"] == question["doc_id"] and c["mapping_status"].startswith("exact_"))
    total = sum(b - a for a, b in gold)
    covered = sum(max(0, min(b, d) - max(a, c)) for a, b in gold for c, d in found)
    return covered / total if total else 0.0


class Bootstrap:
    def __init__(self, repetitions):
        self.repetitions = repetitions
        self.indices = {}
        self.cache = {}

    def describe(self, values):
        values = np.asarray(values, dtype=float)
        key = tuple(values)
        if key in self.cache:
            return dict(self.cache[key])
        n = len(values)
        if n not in self.indices:
            self.indices[n] = np.random.default_rng(SEED + n).integers(0, n, size=(self.repetitions, n))
        simulated = values[self.indices[n]].mean(axis=1)
        result = {"mean": float(values.mean()), "ci95_low": float(np.quantile(simulated, .025)),
                  "ci95_high": float(np.quantile(simulated, .975)), "fact_groups": n}
        self.cache[key] = result
        return dict(result)

    def paired(self, left, right):
        differences = np.asarray(left) - np.asarray(right)
        result = self.describe(differences)
        result.update(wins=int(np.sum(differences > EPS)), ties=int(np.sum(abs(differences) <= EPS)),
                      losses=int(np.sum(differences < -EPS)),
                      ci_contains_zero=result["ci95_low"] <= 0 <= result["ci95_high"])
        return result


def quality_front(candidates, summaries, view):
    dominated = {}
    for left in candidates:
        a = summaries[(left, view, "all")]["metrics"]
        dominated[left] = []
        for right in candidates:
            if left == right:
                continue
            b = summaries[(right, view, "all")]["metrics"]
            if (all(b[m]["mean"] >= a[m]["mean"] - EPS for m in METRICS)
                    and any(b[m]["mean"] > a[m]["mean"] + EPS for m in METRICS)):
                dominated[left].append(right)
    return {"non_dominated": [c for c in candidates if not dominated[c]], "dominated_by": dominated}


def markdown_table(entries):
    lines = ["| 配置 | ContextRecall | ContextPrecision | FactualCorrectness F1 | Faithfulness | 平均上下文本地 tokens | 块数 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for entry in entries:
        metrics = entry["metrics"]
        lines.append("| " + " | ".join([entry["candidate_id"]] +
                     [f"{metrics[m]['mean'] * 100:.2f}%" for m in METRICS] +
                     [f"{entry['resources']['context_local_tokens_mean']:.1f}", str(entry["resources"]["chunk_count"])]) + " |")
    return lines


def audit_judge_traces(grid, rows, configs, qa):
    """Trace exact reference-containment anomalies without regrading answers."""
    contained = [r for r in rows if re.sub(r"\s+", "", r["reference"]) in re.sub(r"\s+", "", r["response"])]
    zero = [r for r in contained if r["metrics"]["FactualCorrectness"]["value"] <= EPS]
    selected, signatures = [], set()
    for row in sorted(zero, key=lambda r: (r["candidate_id"] != "C_structure_s512_o0", r["view"] != VIEWS[0], r["qa_id"], r["candidate_id"])):
        signature = (row["qa_id"], re.sub(r"\s+", "", row["response"]))
        if signature not in signatures:
            signatures.add(signature)
            selected.append(row)
        if len(selected) >= 12:
            break
    targets = defaultdict(set)
    for row in selected:
        config = configs[row["candidate_id"]]
        targets[config["run_id"]].add(row["result_id"] + ":FactualCorrectness")
    events_by_job = defaultdict(list)
    for run_id, jobs in targets.items():
        for line in (grid / "runs" / run_id / "api_events.jsonl").read_text().splitlines():
            event = json.loads(line)
            if event.get("job") in jobs and (event.get("cache_hit") or event.get("status_code") == 200):
                events_by_job[(run_id, event["job"])].append(event)
    traces, cache_hashes = [], {}
    for row in selected:
        config = configs[row["candidate_id"]]
        seen, stages = set(), []
        for event in events_by_job[(config["run_id"], row["result_id"] + ":FactualCorrectness")]:
            cache_id = event["cache_id"]
            if cache_id in seen:
                continue
            seen.add(cache_id)
            cache_path = resolve(config["execution"]["cache_root"]) / (cache_id + ".json")
            response = event.get("response") or load(cache_path)
            if cache_path.exists():
                cache_hashes[relative(cache_path)] = sha(cache_path)
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                continue
            try:
                data = json.loads(choice["message"]["content"])
            except json.JSONDecodeError:
                continue
            stages.append({"cache_id": cache_id, "output": data})
        nlies = [s["output"]["statements"] for s in stages if isinstance(s["output"].get("statements"), list)]
        counts = None
        if len(nlies) >= 2:
            tp = sum(bool(x["verdict"]) for x in nlies[0])
            fp = sum(not bool(x["verdict"]) for x in nlies[0])
            fn = sum(not bool(x["verdict"]) for x in nlies[-1])
            computed = round(2 * tp / (2 * tp + fp + fn), 2) if 2 * tp + fp + fn else 0.0
            counts = {"tp": tp, "fp": fp, "fn": fn, "reconstructed_f1": computed,
                      "matches_saved_f1": abs(computed - row["metrics"]["FactualCorrectness"]["value"]) <= EPS,
                      "failed_response_claims_with_article_reason": sum(not bool(x["verdict"]) and
                          ("article" in x.get("reason", "").lower() or bool(re.search(r"第[^条]{1,12}条", x.get("reason", "")))) for x in nlies[0])}
        question = qa[row["qa_id"]]
        citations = list(dict.fromkeys(re.findall(r"第[^条\s，。；]{1,12}条", row["response"][:100])))
        trace = {"candidate_id": row["candidate_id"], "qa_id": row["qa_id"], "view": row["view"],
                 "fact_group_id": row["pair_group_id"], "saved_factual_f1": row["metrics"]["FactualCorrectness"]["value"],
                 "reference": row["reference"], "response": row["response"],
                 "reference_contained_verbatim_after_whitespace_removal": True,
                 "citation_checks": [{"citation": citation, "present_in_reference": citation in row["reference"],
                                     "present_in_gold_source_spans": any(citation in s["text"] for s in question["source_spans"]),
                                     "present_in_retrieved_context": citation in row["context_rendered"]} for citation in citations],
                 "nli_counts": counts, "unique_call_outputs": stages, "detail_path": row["detail_path"]}
        traces.append(trace)
    return {"scope": "Mechanical reference containment and saved judge trace audit; no new model calls or correctness labels",
            "reference_containment_rows": len(contained), "reference_containment_with_zero_factual_f1_rows": len(zero),
            "zero_f1_containment_fact_groups": sorted({r["pair_group_id"] for r in zero}),
            "zero_f1_containment_query_ids": sorted({r["qa_id"] for r in zero}),
            "selected_trace_cases": traces, "cache_response_sha256": cache_hashes,
            "mechanism": "FactualCorrectness compares response claims against the reference answer, not the retrieved source. Citation prefixes copied into every decomposed claim can make all claims fail despite the core reference being included.",
            "selection_consequence": "Candidate means remain descriptive; do not use FactualCorrectness rankings for final selection before calibration.",
            "calibration_requirement": "Validate source-supported citation prefixes, false citations, extra source-supported facts, missing conditions, and reference scope against real examples under one versioned scoring policy."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, default=ROOT / "pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bootstrap-repetitions", type=int, default=10000)
    args = parser.parse_args()
    grid = args.grid.resolve()
    output = args.output.resolve() if args.output else grid / "analysis_v1"
    output.mkdir(parents=True, exist_ok=True)
    manifest = load(grid / "grid_manifest.json")
    progress = load(grid / "grid_progress.json")
    definitions = {c["candidate_id"]: c for c in manifest["candidate_configs"]}
    run_ids = list(dict.fromkeys(c["run_id"] for c in definitions.values()))
    if len(definitions) != manifest["candidate_count"] or len(run_ids) != 9:
        raise ValueError("Unexpected candidate identities or parameter-run count")
    if any(progress["runs"].get(r, {}).get("status") != "completed" for r in run_ids):
        raise ValueError("Grid is not complete; no candidate recommendation may be produced")

    input_hashes = {relative(grid / name): sha(grid / name) for name in ("grid_manifest.json", "PLAN.md", "AMENDMENT.md")}
    configs, rows_by_candidate, blocks_by_candidate = {}, {}, {}
    invariant_signature = None
    qa = None
    source_texts = {}
    source_paths = {}
    complete_runs = []
    alias_groups = defaultdict(list)
    all_rows = []
    event_totals = {role: Counter() for role in ("embedding", "answer", "judge")}
    event_errors = Counter()
    missing_usage = Counter()
    cache_hits = 0
    length_responses = Counter()
    audit_controls = []
    recovery_records = []

    for run_id in run_ids:
        definitions_for_run = [c for c in definitions.values() if c["run_id"] == run_id]
        config_path = resolve(definitions_for_run[0]["config_path"])
        config = load(config_path)
        directory = grid / "runs" / run_id
        resolved = load(directory / "config.resolved.json")
        summary = load(directory / "summary.json")
        records = load(directory / "results.json")
        selected = load(directory / "selected_qa.json")
        if summary["error_count"] != 0 or summary["status"] != "integration_run_completed_pending_human_review":
            raise ValueError(f"Incomplete or failed final result: {run_id}")
        if digest(config) != resolved["config_sha256"]:
            raise ValueError(f"Config snapshot mismatch: {run_id}")
        if config["split"] != "tuning" or Path(config["qa_file"]).name != "tuning.jsonl":
            raise ValueError("Only the frozen tuning split is allowed")
        invariant = {k: resolved[k] for k in ("qa_file", "qa_sha256", "qa_scope_id", "corpus_manifest", "qa_scope_manifest",
                     "qa_ids", "length", "embedding", "retrieval", "context", "answer", "judge", "code_sha256",
                     "dependency_lock_sha256", "answer_system_prompt", "reference_transform")}
        invariant["chunking_fixed"] = {k: v for k, v in config["chunking"].items() if k not in ("chunk_size", "overlap", "overlap_ratio")}
        invariant["execution_fixed"] = {k: config["execution"][k] for k in ("concurrency", "timeout_seconds", "transport_attempts", "max_new_requests")}
        if invariant_signature is None:
            invariant_signature = digest(invariant)
            qa_path = resolve(config["qa_file"])
            if sha(qa_path) != resolved["qa_sha256"]:
                raise ValueError("Frozen tuning QA hash mismatch")
            qa = {q["qa_id"]: q for q in (json.loads(line) for line in qa_path.read_text().splitlines() if line.strip())}
            if set(qa) != set(config["qa_ids"]) or any(q["split"] != "tuning" for q in qa.values()):
                raise ValueError("Tuning IDs do not match the experiment selection")
            corpus_path = resolve(config["corpus_manifest"])
            corpus = load(corpus_path)
            included_documents = [d for d in corpus["documents"] if d.get("included")]
            if not {q["doc_id"] for q in qa.values()} <= {d["doc_id"] for d in included_documents}:
                raise ValueError("A tuning source document is absent from the index corpus")
            for doc in included_documents:
                path = resolve(doc["canonical_path"])
                if sha(path) != doc["content_sha256"]:
                    raise ValueError("Frozen index corpus source hash mismatch")
                source_paths[doc["doc_id"]] = path
                source_texts[doc["doc_id"]] = path.read_text()
                input_hashes[relative(path)] = sha(path)
            for q in qa.values():
                if sha(source_paths[q["doc_id"]]) != q["content_sha256"]:
                    raise ValueError("QA source hash mismatch")
                if any(source_texts[s["doc_id"]][s["start_char"]:s["end_char"]] != s["text"] for s in q["source_spans"]):
                    raise ValueError("Gold evidence does not map to the frozen canonical source")
            for path in (qa_path, corpus_path, resolve(config["qa_scope_manifest"])):
                input_hashes[relative(path)] = sha(path)
            encoder = tiktoken.get_encoding(config["length"]["encoding"])
        elif digest(invariant) != invariant_signature:
            raise ValueError(f"Non-chunking conditions differ across candidates: {run_id}")
        if stable(selected) != stable([qa[qid] for qid in config["qa_ids"]]):
            # Selection order is irrelevant, content is not.
            if {q["qa_id"]: q for q in selected} != qa:
                raise ValueError("Selected QA snapshot mismatch")
        expected = {(qid, strategy, view) for qid in qa for strategy in config["chunking"]["strategies"] for view in VIEWS}
        if len(records) != len(expected) or {(r["qa_id"], r["strategy"], r["view"]) for r in records} != expected:
            raise ValueError(f"Missing or duplicate result records: {run_id}")
        for name in ("config.resolved.json", "results.json", "summary.json", "selected_qa.json", "judge_controls.json", "api_events.jsonl"):
            input_hashes[relative(directory / name)] = sha(directory / name)
        input_hashes[relative(config_path)] = sha(config_path)
        for definition in definitions_for_run:
            cid, strategy = definition["candidate_id"], definition["strategy"]
            if any(definition[k] != config["chunking"][k] for k in ("chunk_size", "overlap", "overlap_ratio")):
                raise ValueError("Candidate manifest and config disagree")
            block_path = directory / f"{strategy}_chunks.json"
            blocks = load(block_path)
            input_hashes[relative(block_path)] = sha(block_path)
            by_id = {b["chunk_id"]: b for b in blocks}
            if len(by_id) != len(blocks):
                raise ValueError("Duplicate chunk IDs")
            for b in blocks:
                if not b["mapping_status"].startswith("exact_") or b["mapped_source_text"] != source_texts[b["doc_id"]][b["source_start"]:b["source_end"]]:
                    raise ValueError("Unresolved or inconsistent source mapping")
                actual_length = len(encoder.encode(b["body_text"], disallowed_special=()))
                if actual_length != b["length_count"] or actual_length > definition["chunk_size"]:
                    raise ValueError("Chunk token measurement mismatch or limit exceeded")
            alias_groups[digest([{k: b[k] for k in ("doc_id", "body_text", "metadata", "source_start", "source_end")} for b in blocks])].append(cid)
            chosen_records = [r for r in records if r["strategy"] == strategy]
            for row in chosen_records:
                if row["answer_status"] != "ok" or set(row["metrics"]) != set(METRICS):
                    raise ValueError("Missing answer or required metric")
                for metric in row["metrics"].values():
                    value = metric["value"]
                    if metric["status"] != "ok" or not math.isfinite(value) or not -EPS <= value <= 1 + EPS:
                        raise ValueError("Invalid metric value")
                q = qa[row["qa_id"]]
                if row["pair_group_id"] != q["pair_group_id"] or row["question_variant"] != q["question_variant"]:
                    raise ValueError("Query pair identity mismatch")
                for b in row["contexts_used"]:
                    original = by_id[b["chunk_id"]]
                    if any(b[k] != original[k] for k in ("doc_id", "body_text", "source_start", "source_end", "mapping_status")):
                        raise ValueError("Model context does not correspond to stored index chunks")
                expected_contexts = [f"[{i}] Source: {b['doc_path']}\n{b['body_text']}" for i, b in enumerate(row["contexts_used"], 1)]
                rendered = "\n\n".join(expected_contexts)
                if rendered != row["context_rendered"] or row["retrieved_contexts"] != expected_contexts:
                    raise ValueError("Logged scoring/model context mismatch")
                if len(encoder.encode(rendered, disallowed_special=())) != row["context_local_tokens"]:
                    raise ValueError("Context token count mismatch")
                if row["view"] == VIEWS[0] and row["context_local_tokens"] > config["context"]["budget_local_tokens"]:
                    raise ValueError("Fixed context budget exceeded")
                if row["view"] == VIEWS[1] and len(row["contexts_used"]) > config["retrieval"]["top_k"]:
                    raise ValueError("Top-k context count exceeded")
                if abs(coverage(q, row["contexts_used"]) - row["evidence_char_coverage_aux"]) > EPS:
                    raise ValueError("Stored auxiliary evidence coverage differs from interval-union recomputation")
                row["candidate_id"] = cid
                row["detail_path"] = relative(directory / (row["result_id"].replace(":", "_") + ".md"))
                all_rows.append(row)
            configs[cid], rows_by_candidate[cid], blocks_by_candidate[cid] = config, chosen_records, blocks
        controls = load(directory / "judge_controls.json")
        for row in controls:
            if any(v["status"] != "ok" for v in row["metrics"].values()):
                raise ValueError("Incomplete judge control scores")
            audit_controls.append({"run_id": run_id, "result_id": row["result_id"], "expected": row["expected"],
                                   "metrics": {m: v["value"] for m, v in row["metrics"].items()}})
        for line in (directory / "api_events.jsonl").read_text().splitlines():
            event = json.loads(line)
            if event.get("cache_hit"):
                cache_hits += 1
            if not event.get("new_api_call"):
                continue
            role, use = event["role"], event.get("usage", {})
            count = event_totals[role]
            count["new_requests"] += 1
            if event.get("error"):
                count["failed_requests"] += 1
                event_errors[str(event["error"]).split(":", 1)[0]] += 1
            elif event.get("status_code") is None:
                count["interrupted_or_unclassified_requests"] += 1
            if use:
                count["usage_returned_requests"] += 1
                count["provider_input_tokens"] += int(use.get("prompt_tokens", use.get("total_tokens", 0) if role == "embedding" else 0))
                count["provider_output_tokens"] += int(use.get("completion_tokens", 0))
            else:
                missing_usage[role] += 1
            choices = (event.get("response") or {}).get("choices", [])
            if choices and choices[0].get("finish_reason") == "length":
                length_responses[role] += 1
        complete_runs.append({"run_id": run_id, "answers": len(records), "main_metric_values": len(records) * len(METRICS),
                              "final_errors": summary["error_count"]})

    groups = defaultdict(dict)
    for q in qa.values():
        groups[q["pair_group_id"]][q["question_variant"]] = q
    fact_ids = sorted(groups)
    variants = ["source_named"] + sorted({q["question_variant"] for q in qa.values()} - {"source_named"})
    if len(fact_ids) != manifest["fact_group_count"] or len(qa) != manifest["query_variant_count"] or len(variants) != 2:
        raise ValueError("Fact/query counts or variant cardinality mismatch")
    for pair in groups.values():
        if set(pair) != set(variants):
            raise ValueError("Missing query variant in a fact group")
        left, right = (pair[v] for v in variants)
        if any(left[k] != right[k] for k in ("reference_answer", "source_spans", "doc_id", "content_sha256")):
            raise ValueError("Paired query variants do not share facts/evidence")
    for path in sorted((grid / "recovery_checkpoints").glob("*/recovery.json")):
        note = load(path)
        recovery_records.append({"path": relative(path), **note})
        input_hashes[relative(path)] = sha(path)
    bootstrap = Bootstrap(args.bootstrap_repetitions)
    arrays = {}
    summaries = {}
    metric_entries = []
    flat_metrics = []
    group_matrix = []
    diagnostic_rows = []
    diagnostic_counts = Counter()

    for cid, candidate_rows in rows_by_candidate.items():
        definition, blocks = definitions[cid], blocks_by_candidate[cid]
        for view in VIEWS:
            rows = [r for r in candidate_rows if r["view"] == view]
            by_pair = {(r["pair_group_id"], r["question_variant"]): r for r in rows}
            for metric in METRICS:
                arrays[(cid, view, metric)] = np.array([[by_pair[(g, v)]["metrics"][metric]["value"] for v in variants] for g in fact_ids])
            for stratum, column in (("all", None), ("named", 0), ("titlefree", 1)):
                subset = rows if column is None else [r for r in rows if r["question_variant"] == variants[column]]
                descriptions = {m: bootstrap.describe(arrays[(cid, view, m)].mean(axis=1) if column is None
                                else arrays[(cid, view, m)][:, column]) for m in METRICS}
                resources = {"chunk_count": len(blocks), "index_local_tokens": sum(b["length_count"] for b in blocks),
                             "unique_embedding_text_count": len({b["body_text"] for b in blocks}),
                             "context_local_tokens_mean": mean([r["context_local_tokens"] for r in subset]),
                             "selected_chunks_mean": mean([len(r["contexts_used"]) for r in subset]),
                             "answer_final_provider_input_tokens_mean": mean([r["answer_usage"]["prompt_tokens"] for r in subset]),
                             "answer_final_provider_output_tokens_mean": mean([r["answer_usage"]["completion_tokens"] for r in subset])}
                diagnostic = {"char_coverage_mean": mean([r["evidence_char_coverage_aux"] for r in subset]),
                              "zero_char_coverage_queries": sum(r["evidence_char_coverage_aux"] <= EPS for r in subset)}
                entry = {"candidate_id": cid, "strategy": definition["strategy"], "chunk_size": definition["chunk_size"],
                         "overlap": definition["overlap"], "overlap_ratio": definition["overlap_ratio"], "view": view,
                         "stratum": stratum, "query_count": len(subset), "fact_group_count": len(fact_ids),
                         "metrics": descriptions, "resources": resources, "auxiliary_diagnostic": diagnostic}
                summaries[(cid, view, stratum)] = entry
                metric_entries.append(entry)
                for m in METRICS:
                    flat_metrics.append({k: entry[k] for k in ("candidate_id", "strategy", "chunk_size", "overlap", "view", "stratum", "query_count", "fact_group_count")} |
                                        {"metric": m, **descriptions[m], **resources, **diagnostic})
            for group in fact_ids:
                index = fact_ids.index(group)
                group_matrix.append({"candidate_id": cid, "view": view, "fact_group_id": group,
                                     "metrics_pair_mean": {m: float(arrays[(cid, view, m)][index].mean()) for m in METRICS},
                                     "metrics_named": {m: float(arrays[(cid, view, m)][index, 0]) for m in METRICS},
                                     "metrics_titlefree": {m: float(arrays[(cid, view, m)][index, 1]) for m in METRICS}})
            for row in rows:
                scores = {m: row["metrics"][m]["value"] for m in METRICS}
                cues = re.findall(r"证据不足|证据不充分|材料不足|信息不足|无法确定|无法确认|无法回答|未提供|未包含", row["response"])
                flags = []
                if scores["FactualCorrectness"] <= EPS:
                    flags.append("factual_f1_zero")
                if scores["FactualCorrectness"] <= .5:
                    flags.append("factual_f1_le_half")
                if scores["ContextRecall"] >= 1 - EPS and scores["FactualCorrectness"] <= .5:
                    flags.append("full_llm_context_recall_but_low_answer_f1")
                if row["evidence_char_coverage_aux"] <= EPS and scores["ContextRecall"] >= 1 - EPS:
                    flags.append("zero_known_source_coverage_but_full_llm_context_recall")
                if scores["Faithfulness"] >= 1 - EPS and scores["FactualCorrectness"] <= EPS:
                    flags.append("full_faithfulness_but_zero_answer_f1")
                if cues:
                    flags.append("refusal_or_missing_evidence_wording_cue")
                if (re.sub(r"\s+", "", row["reference"]) in re.sub(r"\s+", "", row["response"])
                        and scores["FactualCorrectness"] <= EPS):
                    flags.append("reference_contained_verbatim_but_factual_f1_zero")
                diagnostic_counts.update(flags)
                diagnostic_rows.append({"candidate_id": cid, "view": view, "qa_id": row["qa_id"],
                                        "fact_group_id": row["pair_group_id"], "question_variant": row["question_variant"],
                                        "metrics": scores, "char_coverage_aux": row["evidence_char_coverage_aux"],
                                        "flags": flags, "wording_cues": sorted(set(cues)), "detail_path": row["detail_path"]})

    paired, paired_flat = [], []

    def compare(kind, left, right, view, stratum="all", selected_indices=None):
        column = {"named": 0, "titlefree": 1}.get(stratum)
        for metric in METRICS:
            la, ra = arrays[(left, view, metric)], arrays[(right, view, metric)]
            la = la.mean(axis=1) if column is None else la[:, column]
            ra = ra.mean(axis=1) if column is None else ra[:, column]
            if selected_indices is not None:
                la, ra = la[selected_indices], ra[selected_indices]
            result = {"comparison": kind, "left": left, "right": right, "view": view, "stratum": stratum,
                      "metric": metric, "direction": "left_minus_right", **bootstrap.paired(la, ra)}
            paired.append(result)
            paired_flat.append(result)
        return paired[-len(METRICS):]

    for run_id in run_ids:
        ids = {c["strategy"]: c["candidate_id"] for c in definitions.values() if c["run_id"] == run_id}
        for view in VIEWS:
            for stratum in ("all", "named", "titlefree"):
                compare("matched_parameter_C_minus_B", ids["C_structure"], ids["B_recursive"], view, stratum)
    for cid in definitions:
        for view in VIEWS:
            for metric in METRICS:
                a = arrays[(cid, view, metric)]
                result = {"comparison": "named_minus_titlefree", "left": cid, "right": cid, "view": view,
                          "stratum": "paired_variants", "metric": metric, "direction": "named_minus_titlefree",
                          **bootstrap.paired(a[:, 0], a[:, 1])}
                paired.append(result)
                paired_flat.append(result)

    fronts = {view: quality_front(definitions, summaries, view) for view in VIEWS}
    representatives = {}
    for strategy in ("B_recursive", "C_structure"):
        eligible = [cid for cid, d in definitions.items() if d["strategy"] == strategy]
        representatives[strategy] = sorted(eligible, key=lambda cid: (
            -summaries[(cid, VIEWS[0], "all")]["metrics"]["FactualCorrectness"]["mean"],
            -summaries[(cid, VIEWS[0], "all")]["metrics"]["ContextRecall"]["mean"], cid))[0]
    b, c = representatives["B_recursive"], representatives["C_structure"]
    baseline = "B_recursive_s512_o51"
    for view in VIEWS:
        for stratum in ("all", "named", "titlefree"):
            compare("selected_representatives_C_minus_B", c, b, view, stratum)
            for cid in (b, c):
                compare("representative_minus_starting_config", cid, baseline, view, stratum)
    parameter_pairs = []
    lookup = {(d["strategy"], d["chunk_size"], d["overlap_ratio"]): cid for cid, d in definitions.items()}
    for strategy in representatives:
        for ratio in manifest["overlap_ratios"]:
            for small, large in ((256, 512), (512, 1024), (256, 1024)):
                parameter_pairs.append(("size_sensitivity", lookup[(strategy, large, ratio)], lookup[(strategy, small, ratio)]))
        for size in manifest["size_values_local_tokens"]:
            for low, high in ((0., .1), (0., .2), (.1, .2)):
                parameter_pairs.append(("overlap_sensitivity", lookup[(strategy, size, high)], lookup[(strategy, size, low)]))
    for kind, left, right in parameter_pairs:
        for view in VIEWS:
            compare(kind, left, right, view)

    recovered_groups = sorted({note["target"].split(":")[1].rsplit("_", 1)[0]
                               for note in recovery_records if note.get("target")})
    recovery_indices = [i for i, group in enumerate(fact_ids) if group not in recovered_groups]
    recovery_sensitivity = []
    for cid in definitions:
        metrics = {m: bootstrap.describe(arrays[(cid, VIEWS[0], m)].mean(axis=1)[recovery_indices]) for m in METRICS}
        recovery_sensitivity.append({"candidate_id": cid, "excluded_fact_groups": recovered_groups,
                                     "fact_group_count": len(recovery_indices), "metrics": metrics})
    recovery_comparison = compare("representatives_excluding_technical_recovery_group", c, b, VIEWS[0], selected_indices=recovery_indices)
    loo = []
    for dropped in fact_ids:
        indices = [i for i, g in enumerate(fact_ids) if g != dropped]
        values = {cid: float(arrays[(cid, VIEWS[0], "FactualCorrectness")].mean(axis=1)[indices].mean()) for cid in definitions}
        order = sorted(values, key=lambda cid: (-values[cid], cid))
        loo.append({"excluded_fact_group": dropped, "top_candidate": order[0], "top_factual_f1": values[order[0]],
                    "selected_C_minus_B": values[c] - values[b], "all_candidate_factual_f1": values})
    per_type = []
    group_type = {g: groups[g][variants[0]]["question_type"] for g in fact_ids}
    for qtype in sorted(set(group_type.values())):
        indices = [i for i, g in enumerate(fact_ids) if group_type[g] == qtype]
        for cid in definitions:
            per_type.append({"question_type": qtype, "candidate_id": cid, "fact_group_count": len(indices),
                             "metrics": {m: bootstrap.describe(arrays[(cid, VIEWS[0], m)].mean(axis=1)[indices]) for m in METRICS}})

    # Deterministic, automatic case selection; no new correctness labels.
    case_keys, cases = set(), []
    by_key = {(r["candidate_id"], r["qa_id"], r["view"]): r for r in all_rows}

    def add_case(diag, reason):
        key = (diag["candidate_id"], diag["qa_id"], diag["view"])
        if key in case_keys:
            return
        case_keys.add(key)
        row, question = by_key[key], qa[diag["qa_id"]]
        cases.append({**diag, "selection_reason": reason, "question": row["user_input"], "reference": row["reference"],
                      "response": row["response"], "source_spans": question["source_spans"],
                      "context_local_tokens": row["context_local_tokens"], "selected_chunk_ids": row["retrieved_context_ids"]})

    for cid in (c, b, baseline):
        for variant in variants:
            pool = [d for d in diagnostic_rows if d["candidate_id"] == cid and d["view"] == VIEWS[0] and d["question_variant"] == variant]
            selected = sorted(pool, key=lambda d: (d["metrics"]["FactualCorrectness"], d["char_coverage_aux"], d["qa_id"]))[0]
            add_case(selected, "Representative/start configuration: lowest factual F1 in this query variant")
    anomalies = [d for d in diagnostic_rows if d["view"] == VIEWS[0] and "zero_known_source_coverage_but_full_llm_context_recall" in d["flags"]]
    for selected in sorted(anomalies, key=lambda d: (d["candidate_id"], d["qa_id"]))[:4]:
        add_case(selected, "Known gold source interval absent while LLM ContextRecall is one; alternative evidence may exist")
    long_group = max(fact_ids, key=lambda g: len(groups[g][variants[0]]["source_spans"]))
    for cid in (c, b):
        selected = next(d for d in diagnostic_rows if d["candidate_id"] == cid and d["view"] == VIEWS[0]
                        and d["fact_group_id"] == long_group and d["question_variant"] == variants[1])
        add_case(selected, "Longest evidence-span list in the tuning set; automatic complexity slice")
    for group in recovered_groups:
        selected = next(d for d in diagnostic_rows if d["candidate_id"] == "C_structure_s1024_o102" and d["view"] == VIEWS[1]
                        and d["fact_group_id"] == group and d["question_variant"] == variants[1])
        add_case(selected, "Answer retried once after truncation; original failure retained in recovery checkpoint")

    judge_audit = audit_judge_traces(grid, all_rows, configs, qa)
    anomaly_groups = judge_audit["zero_f1_containment_fact_groups"]
    anomaly_indices = [i for i, group in enumerate(fact_ids) if group not in anomaly_groups]
    anomaly_sensitivity = [{"candidate_id": cid, "excluded_fact_groups": anomaly_groups,
                            "fact_group_count": len(anomaly_indices),
                            "metrics": {m: bootstrap.describe(arrays[(cid, VIEWS[0], m)].mean(axis=1)[anomaly_indices]) for m in METRICS}}
                           for cid in definitions]
    anomaly_comparison = compare("representatives_excluding_reference_containment_anomaly", c, b, VIEWS[0], selected_indices=anomaly_indices)
    representative_comparison = next(p for p in paired if p["comparison"] == "selected_representatives_C_minus_B"
                                     and p["view"] == VIEWS[0] and p["stratum"] == "all" and p["metric"] == "FactualCorrectness")

    integrity = {"status": "passed", "grid_id": manifest["grid_id"], "parameter_runs": complete_runs,
                 "candidate_identities": len(definitions), "distinct_block_sets": len(alias_groups),
                 "block_equivalence_groups": list(alias_groups.values()), "fact_groups": len(fact_ids),
                 "query_variants": len(qa), "index_corpus_documents": len(source_texts),
                 "tuning_question_source_documents": len({q["doc_id"] for q in qa.values()}),
                 "indexed_documents_without_tuning_questions": sorted(set(source_texts) - {q["doc_id"] for q in qa.values()}),
                 "answers": len(all_rows), "main_metric_values": len(all_rows) * len(METRICS),
                 "unresolved_source_chunks": 0, "final_technical_errors": 0,
                 "judge_control_metric_values": sum(len(x["metrics"]) for x in audit_controls),
                 "judge_control_expected_endpoint_matches": sum(abs(v - (1 if 'positive_reference' in x['result_id'] else 0)) <= EPS
                                                                for x in audit_controls for v in x['metrics'].values()),
                 "fixed_conditions_signature": invariant_signature, "holdout_query_file_loaded": False,
                 "qa_review_status": next(iter(configs.values()))["judge"]["review_status"],
                 "input_sha256": input_hashes, "analysis_script_sha256": sha(__file__),
                 "bootstrap": {"repetitions": args.bootstrap_repetitions, "base_seed": SEED,
                               "sample_index_seed_policy": "base_seed + fact_group_count; shared resampling indices within each group count",
                               "unit": "fact group; average paired variants first for pooled results", "multiple_comparison_adjustment": False}}
    usage = {"scope": "All attempts recorded under v3 run directories, including interruptions and the recorded answer recovery; earlier v1/v2 cache creation is not included",
             "new_requests_by_role": {r: dict(v) for r, v in event_totals.items()}, "failed_request_types": dict(event_errors),
             "requests_without_usage_by_role": dict(missing_usage), "recorded_cache_hits": cache_hits,
             "length_limited_responses_by_role": dict(length_responses), "recovery_records": recovery_records,
             "cost_cny": None, "price_or_billing_available": False,
             "warnings": ["Final answer usage in candidate tables excludes earlier truncated calls and transport retries",
                          "API metric latencies include semaphore waits and recovery; do not use them as inference benchmarks",
                          "Judge usage is experiment overhead, not online RAG serving cost",
                          "No usage on failed requests does not establish zero billing; cached requests are not independent repetitions"]}
    sensitivity = {"parameter_comparisons": [p for p in paired if p["comparison"] in ("size_sensitivity", "overlap_sensitivity")],
                   "recovery_exclusion": recovery_sensitivity, "recovery_representative_comparison": recovery_comparison,
                   "scoring_anomaly_group_exclusion": anomaly_sensitivity, "scoring_anomaly_representative_comparison": anomaly_comparison,
                   "leave_one_fact_group_out": loo, "loo_top_candidate_counts": dict(Counter(x["top_candidate"] for x in loo)),
                   "question_type_strata": per_type}
    analysis = {"status": "complete_descriptive_analysis_scoring_calibration_required", "primary_view": VIEWS[0],
                "representatives": representatives, "starting_config": baseline, "quality_frontiers": fronts,
                "candidate_summaries": metric_entries, "paired_comparisons": paired,
                "limitations": ["23 source-law fact groups and 46 controlled query variants; not a general enterprise RAG benchmark",
                                "Four LLM-based metrics, same provider alias for answer and judge; QA and judge independent review pending",
                                "Fact-group bootstrap does not model correlations between related legal topics or judge randomness",
                                "Same-token-budget cap is local cl100k_base, not equal provider input tokens",
                                "No custom pass metric, ordinary IR metrics or coverage gate is used to rank candidates",
                                "Bootstrap and shortlist are post-hoc, no multiple-test correction or independent validation",
                                "One extra identical-request recovery beyond the original truncation fallback is disclosed and excluded in a sensitivity slice",
                                "Saved FactualCorrectness traces reject citation prefixes absent from the reference; final candidate selection requires scoring calibration",
                                "Only tuning queries were loaded; all 29 frozen documents were already indexed, so query holdout is not unseen-document validation",
                                "Final selection requires a locked validation configuration"]}
    diagnostics = {"policy": "Automatic triage conditions only; no new human correctness labels or replacement scores",
                   "threshold_le_half": "0.5 is a case-triage threshold only, not an acceptance gate",
                   "counts_across_all_candidate_view_query_rows": dict(diagnostic_counts), "rows": diagnostic_rows, "selected_cases": cases}
    for name, value in (("integrity.json", integrity), ("metrics.json", {"entries": metric_entries}),
                        ("analysis.json", analysis), ("paired_comparisons.json", paired), ("fact_group_matrix.json", group_matrix),
                        ("sensitivity.json", sensitivity), ("diagnostics.json", diagnostics), ("usage.json", usage),
                        ("judge_controls.json", audit_controls), ("judge_audit.json", judge_audit)):
        save(output / name, value)
    slice_questions = {
        "integrity": ("All configurations, hashes, source mappings and score counts match?", "Passed: 9 runs, 18 distinct block sets, 1,656 answers and 6,624 main metric values", "integrity.json"),
        "metrics": ("Which candidates lead on separate metrics and views?", f"Raw budget factual F1 representatives: {c} {summaries[(c,VIEWS[0],'all')]['metrics']['FactualCorrectness']['mean']:.2%}, {b} {summaries[(b,VIEWS[0],'all')]['metrics']['FactualCorrectness']['mean']:.2%}; rankings require calibration", "metrics.json"),
        "paired": ("Is the representative C/B difference stable at fact-group level?", f"C−B {representative_comparison['mean']*100:+.2f} pp, 95% paired bootstrap [{representative_comparison['ci95_low']*100:+.2f}, {representative_comparison['ci95_high']*100:+.2f}]; exploratory only", "paired_comparisons.json"),
        "sensitivity": ("Do individual facts, parameters and recovery explain the result?", "C/512/0 ranks highest in 21/23 leave-one-group-out slices; recovery exclusion retains a small uncertain difference", "sensitivity.json"),
        "failures": ("Where do answer/reference/score tensions occur?", "Automatic cases and raw NLI traces identify citation-prefix penalties; original scores retained", "diagnostics.json"),
        "judge_audit": ("Can the reference-containment zero F1 be explained by raw judge outputs?", "Reconstructed TP=0, FP=7, FN=0 for source-supported Article 9 citations absent from reference; calibration needed", "judge_audit.json"),
        "resources": ("What resource demand and execution overhead were recorded?", "Final-answer tokens and index size separated from historical transport failures and judge overhead; no billing estimate", "usage.json"),
    }
    slices = [{"slice_id": sid, "status": "completed", "question": question,
               "evaluation_summary": {"takeaway": takeaway, "claim_update": "Raw differences are descriptive; final selection remains unresolved",
                                      "baseline_relation": "Same tuning inputs and frozen grid; B/512/51 retained as starting reference",
                                      "comparability": "Fact-group pairs, separate views and variants, no new model calls",
                                      "failure_mode": "FactualCorrectness citation/reference mismatch; QA and judge review pending",
                                      "next_action": "Calibrate versioned scoring before freezing validation candidates"},
               "evidence_path": name} for sid, (question, takeaway, name) in slice_questions.items()]
    save(output / "slice_summaries.json", slices)
    save(output / "decision.json", {"analysis_id": "zh_paired_grid_analysis_v1", "date": "2026-10-13",
                                   "action": "return_to_scoring_calibration", "final_candidate_selected": False,
                                   "review_representatives": representatives, "holdout_queries_loaded": False,
                                   "reason": "Small uncertain candidate difference plus reproducible citation penalties in the main answer metric",
                                   "next_steps": ["Audit citation and reference-scope examples under a versioned policy",
                                                  "Verify valid citations and false citations with one shared scoring configuration",
                                                  "Apply any revised scoring consistently to candidates, preserving original results",
                                                  "Freeze validation candidates only after calibration and QA review"]})
    save_csv(output / "metrics.csv", flat_metrics)
    save_csv(output / "paired_comparisons.csv", paired_flat)
    primary = sorted([summaries[(cid, VIEWS[0], "all")] for cid in definitions],
                     key=lambda entry: (-entry["metrics"]["FactualCorrectness"]["mean"], entry["candidate_id"]))
    topk = sorted([summaries[(cid, VIEWS[1], "all")] for cid in definitions],
                  key=lambda entry: (-entry["metrics"]["FactualCorrectness"]["mean"], entry["candidate_id"]))
    lines = ["# 中文 Markdown B/C 参数网格：完整结果与配对分析", "", "分析日期：2026-10-13。仅探索性调参结果；QA 与 judge 的独立审核待完成，留出题未读取。", "",
             f"9/9 参数运行、18/18 策略身份完整；实际为 {len(alias_groups)} 套不同块集合。共 {len(all_rows)} 份回答、{len(all_rows)*4} 项主评分，最终技术失败 0。",
             f"实际索引包含全部 {len(source_texts)} 份冻结文档，其中 {len({q['doc_id'] for q in qa.values()})} 份是调参问题来源；其他文档同样参与候选召回。未打开 holdout 问题文件，但这不是文档完全未见的实验设置。",
             "", "## 1. 主要发现与候选建议", "",
             f"评分校准是选型前的必要步骤：{judge_audit['reference_containment_with_zero_factual_f1_rows']} 条记录完整包含参考答案（仅忽略空白），但 FactualCorrectness 为 0。已保存原始 judge 的拆解与 NLI 输出，见 judge_audit.md/json。此筛查不裁决全文正确性；其中已复现来源条号被复制进每条声明、因参考答案缺少条号而全部被否定的机制。",
             f"上述记录涉及 {len(anomaly_groups)} 个事实组（{', '.join(anomaly_groups)}），在配置、问法、上下文视图间重复，并可能复用同一缓存判断；不能把这些记录计作同样数量的独立错误题。",
             f"固定预算下，原始自动事实 F1 的最高均值是 **{c}**；B 策略中最高是 **{b}**。它们是事后选择的重点复核对象；在评分校准前不据此锁定验证配置或宣布最终胜者。",
             "", "| 代表 | 四项质量是否非支配 | 事实 F1 均值及事实组 95% bootstrap 区间 | named F1 | titlefree F1 | 最终有效回答平均 provider 输入 / 输出 tokens |",
             "|---|---|---|---:|---:|---:|"]
    for cid in (c, b, baseline):
        e = summaries[(cid, VIEWS[0], "all")]
        f = e["metrics"]["FactualCorrectness"]
        r = e["resources"]
        lines.append(f"| {cid} | {'是' if cid in fronts[VIEWS[0]]['non_dominated'] else '否'} | {f['mean']:.2%} [{f['ci95_low']:.2%}, {f['ci95_high']:.2%}] | {summaries[(cid,VIEWS[0],'named')]['metrics']['FactualCorrectness']['mean']:.2%} | {summaries[(cid,VIEWS[0],'titlefree')]['metrics']['FactualCorrectness']['mean']:.2%} | {r['answer_final_provider_input_tokens_mean']:.1f} / {r['answer_final_provider_output_tokens_mean']:.1f} |")
    lines.extend(["", f"C 代表减 B 代表的事实 F1 差为 **{representative_comparison['mean']*100:+.2f} 个百分点**；配对区间 [{representative_comparison['ci95_low']*100:+.2f}, {representative_comparison['ci95_high']*100:+.2f}] 个百分点。23 个事实组中 C 较高/相同/较低为 {representative_comparison['wins']}/{representative_comparison['ties']}/{representative_comparison['losses']}。",
                  "区间包含 0 时说明当前事实组不足以区分这两个配置；即使某项区间不含 0，事后挑选、多个比较与单次 LLM judge 仍限制结论。", "",
                  "## 2. 固定 2560 本地 token 上限：18 个身份", "", "下表为 23 个事实组的两种问法等权平均，按事实 F1 展示；排序不等于综合胜负。ContextPrecision 是 Ragas 的排序敏感指标，不是普通 Precision@5。", ""])
    lines.extend(markdown_table(primary))
    lines.extend(["", "四项质量非支配集合：" + "、".join(fronts[VIEWS[0]]["non_dominated"]) + "。", "",
                  "## 3. 固定 top-5：单独诊断", "", "大块在 top-5 视图中通常带入更多文字，不能直接与固定预算视图混合成一个总分。", ""])
    lines.extend(markdown_table(topk))
    lines.extend(["", "## 4. 相同参数 B/C 的事实组配对", "", "方向 C−B；均值与区间的单位为百分点。", "",
                  "| 大小/重叠 | 指标 | C−B | 95% 配对区间 | C 高/平/低事实组 |", "|---|---|---:|---|---:|"])
    for p in paired:
        if p["comparison"] == "matched_parameter_C_minus_B" and p["view"] == VIEWS[0] and p["stratum"] == "all":
            definition = definitions[p["left"]]
            lines.append(f"| {definition['chunk_size']}/{definition['overlap']} | {p['metric']} | {p['mean']*100:+.2f} | [{p['ci95_low']*100:+.2f}, {p['ci95_high']*100:+.2f}] | {p['wins']}/{p['ties']}/{p['losses']} |")
    lines.extend(["", "## 5. 法规名称线索", "", "下面只看两个代表候选的 named−titlefree 配对差异。名称省略是受控消融，不能等同于真实用户全部问法或全行业检索难度。", "",
                  "| 配置 | 指标 | named−titlefree（百分点） | 95% 配对区间 |", "|---|---|---:|---|"])
    for p in paired:
        if p["comparison"] == "named_minus_titlefree" and p["left"] in (b, c) and p["view"] == VIEWS[0]:
            lines.append(f"| {p['left']} | {p['metric']} | {p['mean']*100:+.2f} | [{p['ci95_low']*100:+.2f}, {p['ci95_high']*100:+.2f}] |")
    lines.extend(["", "## 6. 参数与敏感性", "",
                  "所有相同重叠比例下的大小变化、相同大小下的重叠变化见 sensitivity.json 与配对 CSV；没有预设某一重叠比例必然更好。",
                  f"预算装包会改变实际能看到的块数：{b} 在预算视图平均装入 {summaries[(b,VIEWS[0],'all')]['resources']['selected_chunks_mean']:.2f} 块；B/1024/204 仅 {summaries[('B_recursive_s1024_o204',VIEWS[0],'all')]['resources']['selected_chunks_mean']:.2f} 块。同一 B/1024/204 在 top-5 的 ContextRecall 为 {summaries[('B_recursive_s1024_o204',VIEWS[1],'all')]['metrics']['ContextRecall']['mean']:.2%}，预算视图为 {summaries[('B_recursive_s1024_o204',VIEWS[0],'all')]['metrics']['ContextRecall']['mean']:.2%}。这是块粒度与本次预算/装包规则的共同表现，不能推论大块在其他预算下一定差。",
                  "重叠量也没有单调优势：C/512 的 0/51/102 原始事实 F1 依次为 " + "/".join(f"{summaries[(f'C_structure_s512_o{o}',VIEWS[0],'all')]['metrics']['FactualCorrectness']['mean']:.2%}" for o in (0,51,102)) + "；B/256 的 0/25/51 依次为 " + "/".join(f"{summaries[(f'B_recursive_s256_o{o}',VIEWS[0],'all')]['metrics']['FactualCorrectness']['mean']:.2%}" for o in (0,25,51)) + "。这些只是当前原始分数，引用评分校准后还需复查。",
                  f"逐一去掉 23 个事实组后，最高事实 F1 配置的次数为：{stable(sensitivity['loo_top_candidate_counts'])}。这衡量对个别题组的依赖，不是新的独立试验。",
                  f"排除额外技术恢复涉及的整个事实组 {', '.join(recovered_groups)} 后，两代表的 C−B 差为 {recovery_comparison[2]['mean']*100:+.2f} 个百分点，区间 [{recovery_comparison[2]['ci95_low']*100:+.2f}, {recovery_comparison[2]['ci95_high']*100:+.2f}]；四项与全部候选排除结果见 sensitivity.json。", "",
                  f"对全部候选对称排除引用评分异常涉及的事实组后，C−B 事实 F1 差为 {anomaly_comparison[2]['mean']*100:+.2f} 个百分点，区间 [{anomaly_comparison[2]['ci95_low']*100:+.2f}, {anomaly_comparison[2]['ci95_high']*100:+.2f}]；这是事后敏感性检查，不是通过删题修正分数。", "",
                  "## 7. 自动案例筛查与评分边界", "",
                  "低事实 F1、满 ContextRecall 但回答低分、原文覆盖为零却满 ContextRecall、拒答措辞等只作为复核线索。原文未覆盖可能存在其他有效证据；拒答字符串也不是独立正确性标签。原始评分保持不变，没有引入自定义通过率或覆盖门槛参与排序。",
                  f"已生成 {len(cases)} 个可追溯案例，见 cases.md；每条包含原问题、参考答案、原文证据、实际回答、四项原分数与完整上下文链接。所有自动筛查标记见 diagnostics.json。", "",
                  "## 8. 运行恢复与资源记录", "",
                  "第 8 组连接故障恢复后，一条 C/1024/102、titlefree、top-5 回答仍被 1536 上限截断。保存原始截断结果后，对相同请求额外重试一次得到完整回答；该动作超出原先一次长度回退的规则，已公开记录，并做事实组排除敏感性。最终 0 技术失败不表示从未发生请求失败，也不表示回答全正确。",
                  "调用历史按全部 v3 api_events.jsonl 汇总，包括重启前的失败；不只相加最后 attempt_usage。单次有效回答 tokens 不含此前截断/重试消耗。没有配置价格，不估算人民币费用。judge 是评测开销；缓存和排队耗时不能用于宣称线上延迟或线上成本。详情见 usage.json。", "",
                  f"简单正负控制的 {integrity['judge_control_expected_endpoint_matches']}/{integrity['judge_control_metric_values']} 项数值与预期端点一致；这些控制没有覆盖真实引用前缀问题，因此不能据此认证全部业务题的 judge。", "",
                  "## 9. 下一步", "",
                  f"先完成评分校准：用真实样例检查正确引用、错误引用、原文支持的额外内容、遗漏条件及参考答案范围；如调整参考或评分提示词，另存统一版本并对所有比较对象一致应用。保留本轮原始分数。校准后再审核 {c} 与 {b} 的实际错误取舍并锁定验证配置。",
                  "C 的均值领先需要独立确认，不能将这 23 个调参事实当作最终 benchmark。审核完成并冻结验证方案后，再使用原未读取的 holdout 问题；留出仅 7 个事实组，只作初步确认。其来源文档已经在全部 29 份索引中，属于未调参问题验证；未见文档泛化还需要新增独立材料。", "",
                  "## 10. 重现", "", "```bash", "pilot/.venv-rag-eval/bin/python pilot/rag_pipeline/analyze_paired_chunking_grid.py", "```", "",
                  "该命令不调用模型、不读取 holdout、不重写原始 run 结果。分析脚本与输入哈希见 integrity.json；固定 bootstrap 种子及重复数，输出 CSV/JSON/Markdown。", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    metric_lines = ["# 全候选指标", "", "完整数值及事实组 bootstrap 区间见 metrics.csv、metrics.json。", ""]
    for view in VIEWS:
        for stratum in ("all", "named", "titlefree"):
            metric_lines.extend([f"## {view} / {stratum}", ""])
            entries = [summaries[(cid, view, stratum)] for cid in definitions]
            metric_lines.extend(markdown_table(entries))
            metric_lines.append("")
    (output / "metrics.md").write_text("\n".join(metric_lines), encoding="utf-8")
    case_lines = ["# 自动筛查案例包", "", "这些是自动选中的审核线索，不是新增人工正确性标签，也不用于替换主指标。", ""]
    for i, case in enumerate(cases, 1):
        case_lines.extend([f"## {i}. {case['candidate_id']} / {case['qa_id']} / {case['view']}", "",
                           "选择原因：" + case["selection_reason"], "", "自动标记：" + ", ".join(case["flags"]), "",
                           "问题：" + case["question"], "", "参考答案：" + case["reference"], "",
                           "四项原始指标：" + stable(case["metrics"]), "",
                           f"已知原文区间覆盖：{case['char_coverage_aux']:.2%}；上下文 {case['context_local_tokens']} 本地 tokens。", "",
                           "### 实际回答", "", case["response"], "", "### 已标注证据", ""])
        for span in case["source_spans"]:
            case_lines.extend([f"文档 {span['doc_id']}，canonical [{span['start_char']}, {span['end_char']})：", "", "```text", span["text"], "```", ""])
        case_lines.extend([f"[实际上下文与原始评分]({ROOT / case['detail_path']})", ""])
    (output / "cases.md").write_text("\n".join(case_lines), encoding="utf-8")
    audit_lines = ["# FactualCorrectness 原始 judge 输出审计", "", judge_audit["scope"], "",
                   f"1,656 条结果中，有 {judge_audit['reference_containment_rows']} 条回答完整包含参考答案（只去除空白）；其中 {judge_audit['reference_containment_with_zero_factual_f1_rows']} 条事实 F1 为 0。完整包含参考答案不代表回答其余内容必然正确，因此这是机械筛查而非人工正确率。", "",
                   f"这些零分记录涉及 {len(anomaly_groups)} 个事实组，{len(judge_audit['zero_f1_containment_query_ids'])} 个问法，不是 {judge_audit['reference_containment_with_zero_factual_f1_rows']} 道独立题；同一回答或 judge 结果可能通过缓存跨配置复用。",
                   "本地 Ragas 0.4.3 实现先把回答拆成 claims，对照 reference 判断支持，再反向拆解 reference 检查回答覆盖。它没有把 retrieved_contexts 传入 FactualCorrectness。部分回答添加了原文中真实存在的条号，拆解又把条号粘到每条声明；NLI 因 reference 未写条号而将所有正向声明判 0。原始 NLI 数量可重建保存的 F1，详见下列记录。", "",
                   "原始评分和缓存保持可追溯；没有把 0 改为 1，也没有用新的自定义指标替换结果。需要用真实引用及范围对照校准评分，再确定分块配置。", ""]
    for i, trace in enumerate(judge_audit["selected_trace_cases"], 1):
        audit_lines.extend([f"## {i}. {trace['candidate_id']} / {trace['qa_id']} / {trace['view']}", "",
                            "保存的事实 F1：" + str(trace["saved_factual_f1"]), "", "引用位置核对：" + stable(trace["citation_checks"]),
                            "", "原始 NLI 重建：" + stable(trace["nli_counts"]), "", "参考答案：" + trace["reference"],
                            "", "实际回答：" + trace["response"], ""])
        nli = next((stage["output"]["statements"] for stage in trace["unique_call_outputs"] if "statements" in stage["output"]), [])
        if nli:
            audit_lines.extend(["第一条原始 judge 判断（原文，完整列表保存在 JSON）：", "", "```json",
                                json.dumps(nli[0], ensure_ascii=False, indent=2), "```", ""])
        audit_lines.extend([f"[实际上下文与原始评分]({ROOT / trace['detail_path']})", ""])
    (output / "judge_audit.md").write_text("\n".join(audit_lines), encoding="utf-8")
    save(grid / "metrics.json", {"status": analysis["status"], "grid_id": manifest["grid_id"],
                                 "primary_view": VIEWS[0], "metrics": list(METRICS), "fact_groups": len(fact_ids),
                                 "query_variants": len(qa), "candidate_summaries": metric_entries,
                                 "detailed_analysis": relative(output / "report.md"),
                                 "selection_gate": "Score calibration required before final selection; raw metrics retained"})
    (grid / "metrics.md").write_text("# 网格指标入口\n\n所有参数运行已完成；原始自动评分的引用惩罚问题已复现，尚不能最终选型。\n\n"
                                    + f"[完整分析]({output / 'report.md'}) · [原始 judge 审计]({output / 'judge_audit.md'})\n\n"
                                    + "\n".join(metric_lines), encoding="utf-8")
    print(json.dumps({"status": analysis["status"], "candidates": len(definitions), "fact_groups": len(fact_ids),
                      "distinct_block_sets": len(alias_groups), "representatives": representatives,
                      "paired_factual_f1_C_minus_B": representative_comparison, "output": str(output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
