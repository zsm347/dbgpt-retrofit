#!/usr/bin/env python3
"""Freeze a coarse candidate list and check local blocks without model calls."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pilot"))
from rag_pipeline.evaluate_chunking import build_chunks
from rag_pipeline.token_measure import build_length_counter


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> None:
    corpus_path = ROOT / "pilot/data/eval/corpus_manifest.json"
    qa_path = ROOT / "pilot/data/eval/qa_text_tuning_v3_manifest.json"
    corpus = json.loads(corpus_path.read_text())
    qa = json.loads(qa_path.read_text())
    counter = build_length_counter("tiktoken", tiktoken_encoding="cl100k_base")
    candidates, seen = [], {}
    for strategy in ("B_recursive", "C_structure"):
        for size in (256, 512, 1024):
            for ratio in (0, 0.1, 0.2):
                overlap = math.floor(size * ratio)
                candidate_id = f"{strategy}_s{size}_o{overlap}"
                chunks = build_chunks(corpus, strategy, size, overlap, counter)
                lengths = [counter(c["body_text"]) for c in chunks]
                oversize = [c["chunk_id"] for c, n in zip(chunks, lengths) if n > size]
                unmapped = [c["chunk_id"] for c in chunks if c["mapping_status"] == "unresolved"]
                fingerprint = digest([{k:c[k] for k in ["doc_id", "body_text", "metadata", "source_start", "source_end"]} for c in chunks])
                alias = seen.get(fingerprint)
                if not alias:
                    seen[fingerprint] = candidate_id
                candidates.append({"candidate_id": candidate_id, "strategy": strategy, "chunk_size": size,
                                   "overlap": overlap, "overlap_ratio": ratio,
                                   "block_fingerprint": fingerprint, "equivalent_to": alias,
                                   "valid": not oversize and not unmapped, "oversize_ids": oversize, "unresolved_ids": unmapped,
                                   "chunk_count": len(chunks), "max_local_tokens": max(lengths),
                                   "mean_local_tokens": round(statistics.mean(lengths), 2),
                                   "index_local_tokens": sum(lengths),
                                   "size_reason": "Half, equal to, and twice the 512-local-token integration starting point; logarithmic coarse exploration, not industry-mandated or optimal values.",
                                   "overlap_reason": "0 is a no-overlap control; 10% and 20% explore moderate boundary repetition versus redundancy. Floor to integer local tokens; not universal industry standards."})
    output = ROOT / "pilot/experiments/chunking/m4_candidates_v1.json"
    document = {"protocol_id": "m4_candidates_2026-10-09_v1", "status": "local_preflight_complete_model_runs_not_started",
                "corpus_version": corpus["corpus_version"], "corpus_manifest_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
                "qa_scope_id": qa["scope_id"], "qa_sha256": qa["qa_sha256"], "qa_review_status": qa["review_status"],
                "length_spec": counter.spec.as_dict(), "qa_count": qa["qa_count"],
                "declared_candidates": len(candidates), "unique_block_sets": len(seen),
                "valid_candidates": sum(c["valid"] for c in candidates),
                "equivalence_policy": "Reuse exactly identical indexed text/metadata/source-position sets; retain all declared configuration identities. No score-based candidate elimination.",
                "metric_gate": "Before model grid, finish judge controlled diagnostics and freeze evaluation/selection protocol. Human QA/judge review remains pending; exploratory results only.",
                "code_sha256": {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__), ROOT/"pilot/rag_pipeline/chunkers.py", ROOT/"pilot/rag_pipeline/evaluate_chunking.py", ROOT/"pilot/rag_pipeline/token_measure.py"]},
                "candidates": candidates}
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    lines = ["# M4 候选配置与本地块检查", "", "这是参数搜索候选及输入检查，不是 RAG 效果结果或最终参数。", "",
             "B/C × 256/512/1024 个 cl100k_base 本地 tokens × 0/10/20% overlap（向下取整）。大小围绕 M3 的 512 起点减半/加倍；重叠用于考察无重叠、适度重复与更高冗余。均为本项目实验设计，不称作行业标准。", "",
             f"共 {len(candidates)} 个声明配置，{len(seen)} 组独立块集合；{document['valid_candidates']} 个配置通过最终长度/正文映射检查。未发模型请求。", "",
             "| 配置 | 块数 | 平均本地 tokens | 最大本地 tokens | 索引本地 tokens | 等价配置 |", "|---|---:|---:|---:|---:|---|"]
    for row in candidates:
        lines.append(f"| {row['candidate_id']} | {row['chunk_count']} | {row['mean_local_tokens']} | {row['max_local_tokens']} | {row['index_local_tokens']} | {row['equivalent_to'] or '独立'} |")
    lines.extend(["", "C 的 1024 三种重叠配置具有相同的块文本、元数据和来源坐标，因此复用一组计算；这不是根据结果淘汰 C 或某个参数。块数/长度相近不代表等价，等价由完整内容指纹确定。", "",
                  "当前候选保持 M3 的 B 分隔符与 C H1–H3 标题规则；表格按普通 Markdown 处理。256/512/1024 全部检查最终拼接块长，不仅检查名义预算。", "",
                  "正文映射无 unresolved 仅针对这些块；注入标题仍没有独立证据区间，字符覆盖保持辅助。cl100k_base 不是 provider tokenizer，实际 API 限制和用量另记录。", "",
                  "后续必须使用同一 24 题 v3 调参版本、固定模型/检索/上下文及评分协议，对所有有效且独立的候选完成端到端实验。未运行的配置不产生赢家排名；人工审核仍待完成。"])
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"declared":len(candidates), "unique":len(seen), "valid":document["valid_candidates"], "output":str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
