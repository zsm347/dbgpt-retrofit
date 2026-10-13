#!/usr/bin/env python3
"""Map frozen gold evidence spans onto every saved chunking/retrieval result.

Chunk labels here mean positional overlap with the QA's cited gold source span.
Partial overlap is retained explicitly; this mechanical label is not a human
semantic relevance judgment. No models are called and holdout QA is not loaded.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRID = ROOT / "pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3"
VIEWS = ("fixed_local_token_budget", "fixed_top_k")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge(ranges):
    output = []
    for start, end in sorted((int(a), int(b)) for a, b in ranges if b > a):
        if output and start <= output[-1][1]:
            output[-1][1] = max(output[-1][1], end)
        else:
            output.append([start, end])
    return output


def intersection_length(ranges, spans):
    a = merge(ranges)
    b = merge(spans)
    i = j = total = 0
    while i < len(a) and j < len(b):
        total += max(0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID)
    args = parser.parse_args()
    grid = args.grid.resolve()
    out = grid / "evidence_chunk_mapping_v1"
    hashes = {}

    def tracked(path):
        hashes[str(path.relative_to(ROOT))] = sha(path)
        return read(path)

    manifest = tracked(grid / "grid_manifest.json")
    corpus_manifest = tracked(grid / "experiment_corpus_manifest.json")
    docs = {d["doc_id"]: d for d in corpus_manifest["documents"] if d["included"]}
    text_by_doc = {}
    for doc_id, doc in docs.items():
        path = Path(doc["canonical_path"])
        content = path.read_text(encoding="utf-8")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != doc["content_sha256"]:
            raise ValueError(f"Canonical source hash mismatch: {doc_id}")
        hashes[str(path.relative_to(ROOT))] = sha(path)
        text_by_doc[doc_id] = content

    candidates = defaultdict(dict)
    for item in manifest["candidate_configs"]:
        candidates[item["run_id"]][item["strategy"]] = item["candidate_id"]
    qa_all = None
    mapped_cases, per_query = [], []
    coverage_aux_differences = []

    for run_id, strategy_candidates in candidates.items():
        run = grid / "runs" / run_id
        config = tracked(run / "config.resolved.json")
        if config["split"] != "tuning":
            raise ValueError(f"Refusing to load non-tuning QA: {run_id}")
        qa_path = ROOT / config["qa_file"]
        if sha(qa_path) != config["qa_sha256"]:
            raise ValueError(f"Frozen QA hash mismatch: {qa_path}")
        hashes[str(qa_path.relative_to(ROOT))] = sha(qa_path)
        qa_rows = [json.loads(line) for line in qa_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        qa = {q["qa_id"]: q for q in tracked(run / "selected_qa.json")}
        source_qa = {q["qa_id"]: q for q in qa_rows}
        if set(qa) != set(config["qa_ids"]) or any(q != source_qa.get(qid) for qid, q in qa.items()):
            raise ValueError(f"Selected QA differs from frozen tuning data: {run_id}")
        if qa_all is not None and qa != qa_all:
            raise ValueError(f"Question/annotation mismatch: {run_id}")
        qa_all = qa

        chunks_by_strategy = {}
        for strategy in strategy_candidates:
            chunks = tracked(run / f"{strategy}_chunks.json")
            by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
            if len(chunks) != len(by_id):
                raise ValueError(f"Duplicate chunk IDs: {run_id}/{strategy}")
            for chunk in chunks:
                if chunk["doc_id"] not in text_by_doc:
                    raise ValueError(f"Chunk references unknown canonical source: {chunk['chunk_id']}")
                start, end = chunk["source_start"], chunk["source_end"]
                if start < 0 or end > len(text_by_doc[chunk["doc_id"]]) or end < start:
                    raise ValueError(f"Chunk source range out of bounds: {chunk['chunk_id']}")
                if chunk["mapping_status"].startswith("exact_"):
                    if chunk["mapped_source_text"] != text_by_doc[chunk["doc_id"]][start:end]:
                        raise ValueError(f"Chunk source mapping mismatch: {chunk['chunk_id']}")
                else:
                    raise ValueError(f"Non-exact chunk mapping cannot be scored: {chunk['chunk_id']}")
            chunks_by_strategy[strategy] = by_id

        records = tracked(run / "results.json")
        expected = {(s, qid, view) for s in strategy_candidates for qid in qa for view in VIEWS}
        seen = set()
        for record in records:
            strategy, qid, view = record["strategy"], record["qa_id"], record["view"]
            key = (strategy, qid, view)
            if key not in expected or key in seen:
                raise ValueError(f"Unexpected or duplicate result: {run_id}/{key}")
            seen.add(key)
            question = qa[qid]
            if record["answer_status"] != "ok" or record["question_variant"] != question["question_variant"]:
                raise ValueError(f"Incomplete result or mismatched question variant: {run_id}/{key}")
            spans = question["source_spans"]
            if not spans:
                raise ValueError(f"No gold source span for {qid}")
            gold_by_doc = defaultdict(list)
            for span in spans:
                doc_id, start, end = span["doc_id"], span["start_char"], span["end_char"]
                if doc_id not in text_by_doc or doc_id not in question["reference_context_ids"]:
                    raise ValueError(f"Gold span document ID mismatch: {qid}/{span['evidence_id']}")
                if end <= start or text_by_doc[doc_id][start:end] != span["text"]:
                    raise ValueError(f"Gold span is not an exact canonical source range: {qid}/{span['evidence_id']}")
                gold_by_doc[doc_id].append((start, end))
            gold_ranges = {doc: merge(ranges) for doc, ranges in gold_by_doc.items()}
            gold_chars = sum(end - start for ranges in gold_ranges.values() for start, end in ranges)
            chunks = chunks_by_strategy[strategy]
            relevant = {}
            for chunk in chunks.values():
                evidence_ranges = gold_ranges.get(chunk["doc_id"], [])
                overlap = intersection_length([(chunk["source_start"], chunk["source_end"])], evidence_ranges)
                if overlap:
                    fully_covered = [idx for idx, span in enumerate(spans)
                                     if span["doc_id"] == chunk["doc_id"]
                                     and chunk["source_start"] <= span["start_char"]
                                     and chunk["source_end"] >= span["end_char"]]
                    relevant[chunk["chunk_id"]] = {
                        "doc_id": chunk["doc_id"], "chunk_index": chunk["chunk_index"],
                        "source_start": chunk["source_start"], "source_end": chunk["source_end"],
                        "overlap_chars": overlap, "overlap_fraction_of_chunk": overlap / max(1, chunk["source_end"] - chunk["source_start"]),
                        "fully_covers_span_indices": fully_covered,
                    }
            if not relevant:
                raise ValueError(f"No chunk in index intersects gold evidence: {qid}/{strategy}")

            selected = record["contexts_used"]
            if [c["chunk_id"] for c in selected] != record["retrieved_context_ids"]:
                raise ValueError(f"Context/chunk ID mismatch: {run_id}/{key}")
            selected_ids = [c["chunk_id"] for c in selected]
            if len(selected_ids) != len(set(selected_ids)):
                raise ValueError(f"Duplicate selected chunk IDs: {run_id}/{key}")
            for context in selected:
                chunk = chunks.get(context["chunk_id"])
                if not chunk or chunk["doc_id"] != context["doc_id"]:
                    raise ValueError(f"Selected chunk mapping mismatch: {run_id}/{key}")
                if (context["source_start"], context["source_end"]) != (chunk["source_start"], chunk["source_end"]):
                    raise ValueError(f"Selected chunk source range mismatch: {run_id}/{key}")

            selected_relevant = [cid for cid in selected_ids if cid in relevant]
            retrieved_ranges = defaultdict(list)
            retrieved_chars = 0
            for context in selected:
                start, end = context["source_start"], context["source_end"]
                retrieved_ranges[context["doc_id"]].append((start, end))
                retrieved_chars += end - start  # count duplicate overlap as repeated context tokens
            covered_chars = sum(intersection_length(ranges, gold_ranges.get(doc, []))
                                for doc, ranges in retrieved_ranges.items())
            computed_evidence_recall = covered_chars / gold_chars
            stored_coverage = record.get("evidence_char_coverage_aux")
            if stored_coverage is not None:
                coverage_aux_differences.append(abs(computed_evidence_recall - stored_coverage))
                if abs(computed_evidence_recall - stored_coverage) > 1e-8:
                    raise ValueError(f"Evidence coverage differs from stored auxiliary metric: {run_id}/{key}")
            selected_complete_spans = [idx for idx, span in enumerate(spans)
                                       if intersection_length(retrieved_ranges.get(span["doc_id"], []),
                                                              [(span["start_char"], span["end_char"])])
                                       == span["end_char"] - span["start_char"]]
            row = {
                "candidate_id": strategy_candidates[strategy], "run_id": run_id,
                "strategy": strategy, "qa_id": qid, "pair_group_id": question["pair_group_id"],
                "variant": record["question_variant"], "view": view,
                "gold_evidence_span_count": len(spans), "gold_evidence_chars": gold_chars,
                "indexed_evidence_overlap_chunk_count": len(relevant),
                "retrieved_chunk_count": len(selected_ids), "retrieved_evidence_overlap_chunk_count": len(selected_relevant),
                "chunk_precision_overlap": len(selected_relevant) / len(selected_ids) if selected_ids else 0.0,
                "chunk_recall_overlap": len(selected_relevant) / len(relevant),
                "evidence_char_recall": computed_evidence_recall,
                "evidence_char_precision": covered_chars / retrieved_chars if retrieved_chars else 0.0,
                "retrieved_source_chars_including_overlap": retrieved_chars,
                "fully_covered_evidence_span_count": len(selected_complete_spans),
                "selected_complete_span_indices": selected_complete_spans,
                "all_gold_spans_fully_covered": len(selected_complete_spans) == len(spans),
                "indexed_evidence_overlap_chunk_ids": sorted(relevant),
                "retrieved_evidence_overlap_chunk_ids": selected_relevant,
            }
            per_query.append(row)
            mapped_cases.append({
                "candidate_id": row["candidate_id"], "qa_id": qid,
                "pair_group_id": row["pair_group_id"], "view": view,
                "gold_spans": [{"evidence_id": span["evidence_id"], "doc_id": span["doc_id"],
                                "article_number": span["article_number"], "start_char": span["start_char"],
                                "end_char": span["end_char"], "text": span["text"]} for span in spans],
                "indexed_evidence_overlap_chunks": relevant,
                "retrieved_chunk_ids": selected_ids,
                "retrieved_evidence_overlap_chunk_ids": selected_relevant,
            })
        if seen != expected:
            raise ValueError(f"Missing retrieval results: {run_id}")

    summaries = []
    for candidate in manifest["candidate_configs"]:
        for view in VIEWS:
            subset = [r for r in per_query if r["candidate_id"] == candidate["candidate_id"] and r["view"] == view]
            grouped = defaultdict(list)
            for row in subset:
                grouped[row["pair_group_id"]].append(row)
            if len(grouped) != 23 or any(len(v) != 2 for v in grouped.values()):
                raise ValueError(f"Expected 23 paired fact groups: {candidate['candidate_id']}/{view}")
            def pair_macro(key):
                return mean(mean(row[key] for row in group) for group in grouped.values())
            summaries.append({
                "candidate_id": candidate["candidate_id"], "view": view,
                "query_count": len(subset), "fact_group_count": len(grouped),
                "chunk_precision_overlap_macro": pair_macro("chunk_precision_overlap"),
                "chunk_recall_overlap_macro": pair_macro("chunk_recall_overlap"),
                "evidence_char_precision_macro": pair_macro("evidence_char_precision"),
                "evidence_char_recall_macro": pair_macro("evidence_char_recall"),
                "all_gold_spans_fully_covered_rate": pair_macro("all_gold_spans_fully_covered"),
                "mean_indexed_evidence_overlap_chunks": pair_macro("indexed_evidence_overlap_chunk_count"),
                "mean_retrieved_chunks": pair_macro("retrieved_chunk_count"),
            })

    if any(sha(ROOT / relative) != digest for relative, digest in hashes.items()):
        raise ValueError("An input changed during mapping")
    unique_group_qa = {q["pair_group_id"]: q for q in qa_all.values()}
    unique_spans = [span for q in unique_group_qa.values() for span in q["source_spans"]]
    granularity_counts = Counter(span["evidence_granularity"] for span in unique_spans)
    minimum_overlap = min(item["overlap_chars"] for case in mapped_cases
                          for item in case["indexed_evidence_overlap_chunks"].values())
    out.mkdir(exist_ok=True)
    save(out / "evidence_chunk_map.json", mapped_cases)
    save(out / "per_query.json", per_query)
    save(out / "metrics.json", summaries)
    with (out / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    save(out / "provenance.json", {
        "status": "mechanical_source_span_mapping_complete_semantic_review_not_implied",
        "chunk_relevance_rule": "positive_char_intersection_with_existing_gold_source_spans",
        "partial_overlap_chunks_count_as_relevant_for_chunk_precision_recall": True,
        "evidence_character_recall": "unique gold source characters covered by selected chunk ranges / unique gold source characters",
        "evidence_character_precision": "unique gold source characters covered / sum of selected source-range characters; overlap duplication remains in denominator",
        "gold_spans_are_article_level_in_many_questions": True,
        "candidate_count": len(manifest["candidate_configs"]), "mapped_case_count": len(mapped_cases),
        "per_query_count": len(per_query), "summary_count": len(summaries),
        "fact_groups": len({q["pair_group_id"] for q in qa_all.values()}),
        "source_spans": {"question_variant_records": sum(len(q["source_spans"]) for q in qa_all.values()),
                         "distinct_pair_group_spans": len(unique_spans),
                         "granularity_counts_by_pair_group": dict(granularity_counts)},
        "minimum_positive_chunk_span_overlap_chars": minimum_overlap,
        "stored_evidence_char_coverage_crosscheck": {
            "comparisons": len(coverage_aux_differences),
            "mismatches_over_1e-8": sum(diff > 1e-8 for diff in coverage_aux_differences),
            "maximum_absolute_difference": max(coverage_aux_differences, default=0.0),
        },
        "input_sha256": hashes, "script_sha256": sha(Path(__file__)),
        "new_model_calls": 0, "holdout_questions_loaded": False, "original_results_modified": False,
        "method_reference": "https://www.trychroma.com/research/evaluating-chunking",
    })
    report = [
        "# 证据映射到分块结果 v1", "",
        "## 做了什么", "",
        "对 23 个事实组、46 个问题、18 个候选分块结果，将评测集中原文证据的固定字符区间映射到每个配置的全量块和实际召回块。配对问法共享证据，因此有 29 个不同的证据区间、58 条问题记录中的区间；对应 828 组候选问题/分块映射和 1,656 条逐题/视图计算。不调用模型，不加载 holdout。", "",
        "区间是 canonical 原文中的半开区间 `[start_char, end_char)`。脚本逐条核对证据文本等于原文切片；逐块核对块 ID、来源文档和原文区间。块只要与 gold evidence 区间相交就计入机械意义上的“证据相交块”，并另外保存相交字符数/比例以及完整覆盖哪些证据区间。", "",
        "## 指标口径", "",
        "- **证据相交块 Precision**：实际召回且与 gold evidence 有字符交集的块数 / 实际召回块数。",
        "- **证据相交块 Recall**：实际召回且与 gold evidence 有字符交集的块数 / 该配置全索引中与 gold evidence 有字符交集的块数。",
        "- **证据字符 Recall**：已召回块覆盖的 gold evidence 不重复字符数 / gold evidence 不重复字符总数。",
        "- **证据字符 Precision**：已召回块覆盖的 gold evidence 不重复字符数 / 召回块所含原文字符数之和；overlap 重复字符仍在分母中。",
        "- “完整覆盖率”表示一题的全部已标注证据区间均被召回区间覆盖。", "",
        "两个问法按事实组先平均，再对 23 组宏平均；top-5 与 2560 本地 token 预算分开计算。", "",
        "## 预算视图汇总", "",
        "| 配置 | 相交块 P | 相交块 R | 证据字符 P | 证据字符 R | 全证据区间覆盖率 | 平均索引相关块 | 平均召回块 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        if row["view"] == "fixed_local_token_budget":
            report.append(f"| {row['candidate_id']} | {row['chunk_precision_overlap_macro']:.2%} | "
                          f"{row['chunk_recall_overlap_macro']:.2%} | {row['evidence_char_precision_macro']:.2%} | "
                          f"{row['evidence_char_recall_macro']:.2%} | {row['all_gold_spans_fully_covered_rate']:.2%} | "
                          f"{row['mean_indexed_evidence_overlap_chunks']:.2f} | {row['mean_retrieved_chunks']:.2f} |")
    report += ["", "top-5 汇总见 `metrics.csv` / `metrics.json`。", "",
        "## 如何理解与限制", "",
        "普通 IR 框架常把每个检索节点/块 ID 与人工给定的相关块 ID 比较；这份结果则先用已有的来源区间做自动对齐，属于可复现的块级初步指标。块级判定目前采用‘与证据区间有任何字符相交’；所以只覆盖一个字符的块也算相交块。相交块 P/R 不能当成已经人工确认的语义相关性。", "",
        "当前 29 个不同 QA evidence 区间中，28 个标为 article、1 个标为 paragraph。大多数问题的答案与所引条文逐项对应，但有的问题只问条文的一部分，整条条文会把不必要内容也纳入 gold 区间。因此，本报告同时提供基于原文位置的字符 P/R 和证据区间全覆盖率，便于发现只命中来源而漏掉证据的情况；正式结论仍应抽查或修订超出问题所需范围的证据区间。字符单位是字符，不应解释为 tokenizer token 指标。", "",
        "证据字符 Recall 另与之前保存的 `evidence_char_coverage_aux` 逐题对照：1,656/1,656 完全一致（最大差值0）。当前已映射的正向块与证据 span 最小相交长度为20字符；虽然没有单字符擦边案例，‘只要相交即算相关’仍是机械标签。", "",
        "例：C/512/0 的 `ragas_zh_v1_0023_titlefree` 找到了标注来源文档（文档 Recall=100%），但该题标注的560字符证据区间没有与任何已召回块相交（证据字符 Recall=0%；证据相交块 Recall=0%）。这具体展示了来源命中和证据命中的区别。", "",
        "候选配置间 chunk 数不同，重叠会让同一证据落入多个块，所以块级 Recall 的分母也随分块方案变化；它回答的是‘这个配置索引中的证据相交块找回多少’，不是跨配置同一固定块的对比。固定原文证据字符 Recall 更直接比较不同切法对证据保留/找回的影响。", "",
        "## 文件", "",
        "- `evidence_chunk_map.json`：每个配置/问题的原始证据区间及全索引相交块清单，含重叠比例和完整覆盖span编号。",
        "- `per_query.json`：逐题分子、分母和指标。",
        "- `metrics.json` / `metrics.csv`：18配置×2视图汇总。",
        "- `provenance.json`：输入/代码 SHA-256、范围及计算口径。", "",
        "## 重现", "",
        "```bash", "pilot/.venv-rag-eval/bin/python pilot/rag_pipeline/map_evidence_to_chunks.py", "```", "",
        "指标取向参考 Chroma 的分块评测报告：按原文 token 证据区间评估召回和冗余。这里因现成标注为字符 offset，采用字符区间，不冒称 token 级结果。[Chroma technical report](https://www.trychroma.com/research/evaluating-chunking)", ""]
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
    selected = [r for r in summaries if r["view"] == "fixed_local_token_budget"
                and r["candidate_id"] in {"C_structure_s512_o0", "B_recursive_s256_o51"}]
    print(json.dumps({"output": str(out), "mapping_cases": len(mapped_cases), "query_view_rows": len(per_query),
                      "summaries": len(summaries), "budget_representatives": selected}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
