#!/usr/bin/env python3
"""Run a local, model-free chunking retrieval diagnostic.

This runner compares the existing B/C chunkers using a deterministic TF-IDF
cosine retriever.  It is intentionally a pipeline smoke test and retrieval
diagnostic: it does not produce answer-quality, faithfulness, or Ragas scores.
The output keeps source offsets and evidence coverage so later embedding/Ragas
adapters can reuse the same run layout.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "pilot/data/eval/corpus_manifest.json"
DEFAULT_QA = PROJECT_ROOT / "pilot/data/eval/qa_source_reviewed_v2.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "pilot/experiments/chunking"

# B/C only. A is retained in chunkers.py for historical comparisons but is
# deliberately excluded from this experiment line.
STRATEGY_NAMES = ("B_recursive", "C_structure")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tokenize(value: str) -> list[str]:
    return [item.casefold() for item in TOKEN_RE.findall(value)]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return []
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def interval_length(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in merge_intervals(intervals))


def intersection_length(
    left: Iterable[tuple[int, int]], right: Iterable[tuple[int, int]]
) -> int:
    left_merged = merge_intervals(left)
    right_merged = merge_intervals(right)
    total = 0
    for left_start, left_end in left_merged:
        for right_start, right_end in right_merged:
            start = max(left_start, right_start)
            end = min(left_end, right_end)
            if end > start:
                total += end - start
    return total


class TfidfIndex:
    """Small deterministic TF-IDF index for offline diagnostics."""

    def __init__(self, texts: list[str]):
        self.doc_terms = [collections.Counter(tokenize(text)) for text in texts]
        document_frequency: collections.Counter[str] = collections.Counter()
        for terms in self.doc_terms:
            document_frequency.update(terms.keys())
        self.document_count = len(texts)
        self.idf = {
            token: math.log((self.document_count + 1) / (frequency + 0.5)) + 1.0
            for token, frequency in document_frequency.items()
        }
        self.doc_norms = [self._norm(terms) for terms in self.doc_terms]

    def _norm(self, terms: collections.Counter[str]) -> float:
        return math.sqrt(
            sum((count * self.idf[token]) ** 2 for token, count in terms.items() if token in self.idf)
        )

    def score(self, query: str, index: int) -> float:
        query_terms = collections.Counter(tokenize(query))
        query_norm = self._norm(query_terms)
        if query_norm == 0 or self.doc_norms[index] == 0:
            return 0.0
        dot = sum(
            query_count * self.idf[token] * self.doc_terms[index][token] * self.idf[token]
            for token, query_count in query_terms.items()
            if token in self.doc_terms[index]
        )
        return dot / (query_norm * self.doc_norms[index])

    def rank(self, query: str) -> list[tuple[int, float]]:
        scored = [(index, self.score(query, index)) for index in range(len(self.doc_terms))]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored


def get_chunker(strategy: str):
    # Import lazily so metadata commands can still run if the optional pilot
    # environment has not been activated.
    sys.path.insert(0, str(PROJECT_ROOT / "pilot"))
    from rag_pipeline import chunkers

    try:
        return getattr(chunkers, {"B_recursive": "split_recursive", "C_structure": "split_structure_aware"}[strategy])
    except KeyError as exc:
        raise ValueError(f"Unsupported strategy {strategy}; choose from {STRATEGY_NAMES}") from exc


def get_length_counter(args: argparse.Namespace):
    """Construct the explicitly selected length counter.

    The legacy diagnostic remains character based by default.  Token backends
    are opt-in and fail loudly when their tokenizer dependency or identifier
    is missing; this prevents a formal token run from silently using chars.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "pilot"))
    from rag_pipeline.token_measure import build_length_counter

    return build_length_counter(
        args.length_unit,
        tiktoken_encoding=args.tiktoken_encoding,
        tiktoken_model=args.tiktoken_model,
        tokenizer_name=args.tokenizer_name,
        revision=args.tokenizer_revision,
        local_files_only=args.local_files_only,
    )


def build_chunks(
    manifest: dict[str, Any], strategy: str, chunk_size: int, overlap: int,
    length_counter=None,
) -> list[dict[str, Any]]:
    chunker = get_chunker(strategy)
    if length_counter is None:
        length_counter = lambda value: len(value)
    chunks: list[dict[str, Any]] = []
    for document in manifest["documents"]:
        if not document.get("included"):
            continue
        source_path = Path(document["canonical_path"])
        text = source_path.read_text(encoding="utf-8")
        actual_sha = sha256_file(source_path)
        if actual_sha != document["content_sha256"]:
            raise ValueError(
                f"Source hash mismatch for {document['path']}: "
                f"manifest={document['content_sha256']}, actual={actual_sha}"
            )
        produced = chunker(
            text,
            document["doc_id"],
            chunk_size=chunk_size,
            overlap=overlap,
            protect=False,
            length_function=length_counter,
        )
        for index, chunk in enumerate(produced):
            start = int(chunk.start) if chunk.start is not None else -1
            end = int(chunk.end) if chunk.end is not None else -1
            mapping_status = getattr(chunk, "mapping_status", "unresolved")
            if (
                start < 0 or end <= start or end > len(text)
                or mapping_status == "unresolved"
            ):
                mapping_status = "unresolved"
                start, end = -1, -1
            chunks.append(
                {
                    "chunk_id": f"{strategy}:{document['doc_id']}:{index:04d}",
                    "doc_id": document["doc_id"],
                    "doc_path": document["path"],
                    "strategy": strategy,
                    "chunk_index": index,
                    "body_text": chunk.content,
                    "metadata": chunk.metadata,
                    "source_start": start,
                    "source_end": end,
                    "char_count": len(chunk.content),
                    "mapping_status": mapping_status,
                    "mapped_source_text": text[start:end] if start >= 0 else None,
                    "length_count": length_counter(chunk.content),
                }
            )
    return chunks


def span_overlap(chunk: dict[str, Any], qa: dict[str, Any]) -> int:
    if (
        chunk["doc_id"] != qa["doc_id"]
        or not chunk["mapping_status"].startswith("exact_")
    ):
        return 0
    chunk_interval = [(chunk["source_start"], chunk["source_end"])]
    gold_intervals = [
        (int(span["start_char"]), int(span["end_char"]))
        for span in qa["source_spans"]
    ]
    return intersection_length(chunk_interval, gold_intervals)


def evaluate_qa(
    qa: dict[str, Any], chunks: list[dict[str, Any]], index: TfidfIndex, top_k: int
) -> dict[str, Any]:
    ranked = index.rank(qa["question"])
    selected = ranked[:top_k]
    retrieved = [chunks[index_] for index_, _ in selected]
    gold_intervals = [
        (int(span["start_char"]), int(span["end_char"]))
        for span in qa["source_spans"]
    ]
    gold_length = interval_length(gold_intervals)
    overlaps = [span_overlap(chunk, qa) for chunk in retrieved]
    first_evidence_rank = next((rank + 1 for rank, value in enumerate(overlaps) if value > 0), None)
    retrieved_intervals = [
        (chunk["source_start"], chunk["source_end"])
        for chunk in retrieved
        if chunk["doc_id"] == qa["doc_id"]
        and chunk["mapping_status"].startswith("exact_")
    ]
    covered_length = intersection_length(retrieved_intervals, gold_intervals)
    coverage = covered_length / gold_length if gold_length else 0.0
    return {
        "qa_id": qa["qa_id"],
        "doc_id": qa["doc_id"],
        "doc_path": qa["doc_path"],
        "question": qa["question"],
        "split": qa["split"],
        "question_type": qa["question_type"],
        "gold_evidence_chars": gold_length,
        "retrieved_evidence_chars": covered_length,
        "first_evidence_rank": first_evidence_rank,
        "evidence_hit": first_evidence_rank is not None,
        "evidence_coverage": round(coverage, 6),
        "retrieved": [
            {
                "rank": rank + 1,
                "chunk_id": chunk["chunk_id"],
                "doc_id": chunk["doc_id"],
                "doc_path": chunk["doc_path"],
                "score": round(score, 8),
                "source_start": chunk["source_start"],
                "source_end": chunk["source_end"],
                "mapping_status": chunk["mapping_status"],
                "overlap_chars": overlaps[rank],
                "mapped_source_text": chunk["mapped_source_text"],
                "text": chunk["body_text"],
            }
            for rank, (chunk, (_, score)) in enumerate(zip(retrieved, selected))
        ],
    }


def summarize(results: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    total = len(results)
    if not total:
        return {"questions": 0}
    hits = [item["evidence_hit"] for item in results]
    coverages = [item["evidence_coverage"] for item in results]
    ranks = [item["first_evidence_rank"] for item in results if item["first_evidence_rank"] is not None]
    split_stats: dict[str, Any] = {}
    for split in ("tuning", "holdout"):
        subset = [item for item in results if item["split"] == split]
        if not subset:
            continue
        split_stats[split] = {
            "questions": len(subset),
            "evidence_hit_at_k": round(sum(item["evidence_hit"] for item in subset) / len(subset), 6),
            "mean_evidence_coverage": round(statistics.mean(item["evidence_coverage"] for item in subset), 6),
        }
    by_type: dict[str, dict[str, Any]] = {}
    for question_type in sorted({item["question_type"] for item in results}):
        subset = [item for item in results if item["question_type"] == question_type]
        by_type[question_type] = {
            "questions": len(subset),
            "evidence_hit_at_k": round(sum(item["evidence_hit"] for item in subset) / len(subset), 6),
            "mean_evidence_coverage": round(statistics.mean(item["evidence_coverage"] for item in subset), 6),
        }
    return {
        "questions": total,
        "top_k": top_k,
        "evidence_hit_at_k": round(sum(hits) / total, 6),
        "mean_evidence_coverage": round(statistics.mean(coverages), 6),
        "median_first_evidence_rank": statistics.median(ranks) if ranks else None,
        "unresolved_retrieved_chunks": sum(
            not item["mapping_status"].startswith("exact_")
            for result in results
            for item in result["retrieved"]
        ),
        "by_split": split_stats,
        "by_question_type": by_type,
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=STRATEGY_NAMES, required=True)
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--overlap", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--run-id", required=True, help="Stable directory name, e.g. b_recursive_800_100_lexical")
    parser.add_argument(
        "--length-unit",
        choices=("characters", "tiktoken", "transformers"),
        default="characters",
        help="Chunk size unit. characters is the legacy diagnostic; token backends require explicit tokenizer settings.",
    )
    parser.add_argument("--tiktoken-encoding", help="Explicit tiktoken encoding name")
    parser.add_argument("--tiktoken-model", help="Model name only when tiktoken knows its encoding")
    parser.add_argument("--tokenizer-name", help="Transformers model id or local tokenizer path")
    parser.add_argument("--tokenizer-revision", help="Optional transformers revision")
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="For transformers, load only from the local cache",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--qa", type=Path, default=DEFAULT_QA)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    if args.chunk_size <= 0 or args.overlap < 0 or args.overlap >= args.chunk_size:
        raise SystemExit("Require chunk_size > 0 and 0 <= overlap < chunk_size")
    if args.top_k <= 0:
        raise SystemExit("top_k must be positive")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    qa_records = read_jsonl(args.qa)
    if not qa_records:
        raise SystemExit("QA file is empty")
    try:
        length_counter = get_length_counter(args)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    chunks = build_chunks(
        manifest,
        args.strategy,
        args.chunk_size,
        args.overlap,
        length_counter=length_counter,
    )
    if not chunks:
        raise SystemExit("No chunks were produced")
    index = TfidfIndex([chunk["body_text"] for chunk in chunks])
    results = [evaluate_qa(qa, chunks, index, args.top_k) for qa in qa_records]
    summary = summarize(results, args.top_k)
    out_dir = args.output_root / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "schema_version": "0.1",
        "run_id": args.run_id,
        "corpus_version": manifest["corpus_version"],
        "qa_file": str(args.qa),
        "strategy": args.strategy,
        "chunk_size": args.chunk_size,
        "overlap": args.overlap,
        "length_unit": length_counter.spec.unit,
        "length_spec": length_counter.spec.as_dict(),
        "top_k": args.top_k,
        "retriever": "tfidf_cosine_offline_diagnostic",
        "answer_generation": "not_run",
        "llm_metrics": "not_run",
        "mapping_note": "Chunk offsets are emitted only when the splitter text is found in the frozen source after whitespace normalization; unresolved chunks have no fabricated interval. C intervals cover the body and exclude injected heading metadata.",
        "chunk_count": len(chunks),
        "qa_count": len(qa_records),
        "qa_sha256": sha256_text(args.qa.read_text(encoding="utf-8")),
    }
    (out_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_jsonl(out_dir / "chunks.jsonl", chunks)
    write_jsonl(out_dir / "retrieval_results.jsonl", results)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(
        f"""# Chunking retrieval diagnostic: `{args.run_id}`

- Strategy: `{args.strategy}`
- Chunk size / overlap: `{args.chunk_size}` / `{args.overlap}` `{length_counter.spec.unit}`
- Length counter: `{length_counter.spec.identifier}` (`{length_counter.spec.library}` {length_counter.spec.library_version or 'unknown'})
- Retriever: deterministic TF-IDF cosine (offline diagnostic)
- Questions: {len(qa_records)}; top-k: {args.top_k}
- Chunks: {len(chunks)}
- LLM answer generation and Ragas metrics: **not run**

## Summary

```json
{json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)}
```

Evidence hits use the current chunker's source offsets and are diagnostic only. The run records the selected length counter explicitly; a character run must not be relabelled as a token run.
""",
        encoding="utf-8",
    )
    print(json.dumps({"run_id": args.run_id, "chunks": len(chunks), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
