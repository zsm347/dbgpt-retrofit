#!/usr/bin/env python3
"""Audit provider-side token counts for a small frozen-corpus sample.

This is deliberately separate from the local chunker's ``length_function``.
DashScope returns ``usage.total_tokens`` for the whole embedding request, so
the audit sends exactly one input text per request.  The response contains no
embedding data in the saved artifact; only counts, hashes, and request
metadata are retained.

The API key is read from ``DASHSCOPE_API_KEY`` first, then from the selected
DB-GPT TOML profile.  It is never printed or written to the output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import statistics
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "pilot/data/eval/corpus_manifest.json"
DEFAULT_CONFIG = Path.home() / ".dbgpt/configs/qwen.toml"
DEFAULT_MODEL = "text-embedding-v3"
DEFAULT_OUTPUT = PROJECT_ROOT / "pilot/experiments/chunking/provider_token_audit_v1.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_config_credentials(config_path: Path, model: str) -> tuple[str | None, str | None]:
    if not config_path.exists():
        return None, None
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    embeddings = config.get("models", {}).get("embeddings", [])
    for item in embeddings:
        if item.get("name") == model or item.get("backend") == model:
            return item.get("api_key"), item.get("provider")
    return None, None


def response_field(response: Any, name: str) -> Any:
    value = getattr(response, name, None)
    if value is not None:
        return value
    try:
        return response[name]
    except (KeyError, TypeError, AttributeError):
        return None


def provider_total_tokens(response: Any) -> int:
    usage = response_field(response, "usage")
    if usage is None:
        raise RuntimeError("DashScope response did not contain usage")
    value = usage.get("total_tokens") if isinstance(usage, dict) else getattr(usage, "total_tokens", None)
    if value is None:
        try:
            value = usage["total_tokens"]
        except (KeyError, TypeError):
            value = None
    if value is None:
        raise RuntimeError("DashScope response usage did not contain total_tokens")
    return int(value)


def choose_documents(manifest: dict[str, Any], sample_size: int) -> list[dict[str, Any]]:
    documents = [item for item in manifest["documents"] if item.get("included")]
    documents.sort(key=lambda item: (item["chars_nonwhitespace"], item["doc_id"]))
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")
    if sample_size >= len(documents):
        return documents

    # Keep short/median/long coverage and, when possible, one document with
    # the highest Chinese proxy ratio.  Selection is deterministic and based
    # only on the frozen manifest, never on provider results.
    indices = {0, len(documents) // 2, len(documents) - 1}
    cn_index = max(
        range(len(documents)),
        key=lambda index: (
            documents[index].get("language_proxy", {}).get("cn_ratio", 0.0),
            -documents[index]["chars_nonwhitespace"],
        ),
    )
    indices.add(cn_index)
    selected = [documents[index] for index in sorted(indices)]
    if len(selected) > sample_size:
        # Preserve the endpoints first, then the most Chinese candidate.
        keep = {documents[0]["doc_id"], documents[-1]["doc_id"]}
        keep.add(documents[cn_index]["doc_id"])
        selected = [item for item in selected if item["doc_id"] in keep]
    if len(selected) < sample_size:
        for item in documents:
            if item["doc_id"] not in {row["doc_id"] for row in selected}:
                selected.append(item)
            if len(selected) == sample_size:
                break
    return selected[:sample_size]


def load_proxy_counter():
    """Return an optional cl100k proxy counter without making it mandatory."""
    try:
        import tiktoken  # type: ignore[import-not-found]
    except ImportError:
        return None
    encoding = tiktoken.get_encoding("cl100k_base")
    return lambda text: len(encoding.encode(text, allowed_special=set(), disallowed_special=()))


def audit(
    manifest_path: Path,
    config_path: Path,
    model: str,
    sample_size: int,
    api_key: str | None = None,
) -> dict[str, Any]:
    try:
        import dashscope  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("dashscope is not installed in this environment") from exc

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = choose_documents(manifest, sample_size)
    configured_key, provider = load_config_credentials(config_path, model)
    key = api_key or os.environ.get("DASHSCOPE_API_KEY") or configured_key
    if not key:
        raise RuntimeError(
            "No DashScope API key found. Set DASHSCOPE_API_KEY or configure the "
            f"embedding key in {config_path}."
        )

    proxy_counter = load_proxy_counter()
    records: list[dict[str, Any]] = []
    for document in documents:
        path = Path(document["canonical_path"])
        text = path.read_text(encoding="utf-8")
        response = dashscope.TextEmbedding.call(
            model=model,
            input=[text],
            api_key=key,
        )
        status_code = response_field(response, "status_code")
        if status_code not in (None, 200):
            code = response_field(response, "code") or "unknown"
            raise RuntimeError(f"DashScope request failed for {document['path']}: {code}")
        provider_count = provider_total_tokens(response)
        record: dict[str, Any] = {
            "doc_id": document["doc_id"],
            "doc_path": document["path"],
            "content_sha256": sha256_file(path),
            "char_count": len(text),
            "nonwhitespace_char_count": document["chars_nonwhitespace"],
            "provider_token_count": provider_count,
            "provider_token_unit": "DashScope usage.total_tokens",
            "model": model,
            "request_mode": "one_input_per_request",
        }
        if proxy_counter is not None:
            proxy_count = proxy_counter(text)
            record["cl100k_proxy_token_count"] = proxy_count
            record["provider_to_cl100k_ratio"] = round(provider_count / proxy_count, 6) if proxy_count else None
        records.append(record)

    counts = [record["provider_token_count"] for record in records]
    sorted_counts = sorted(counts)
    proxy_ratios = [
        record["provider_to_cl100k_ratio"]
        for record in records
        if record.get("provider_to_cl100k_ratio") is not None
    ]
    summary: dict[str, Any] = {
        "documents": len(records),
        "total_provider_tokens": sum(counts),
        "min_provider_tokens": min(counts),
        "p50_provider_tokens": sorted_counts[len(sorted_counts) // 2],
        "p90_provider_tokens": sorted_counts[min(len(sorted_counts) - 1, int(len(sorted_counts) * 0.9))],
        "max_provider_tokens": max(counts),
        "mean_provider_tokens": round(sum(counts) / len(counts), 3),
    }
    if proxy_ratios:
        summary.update(
            {
                "min_provider_to_cl100k_ratio": min(proxy_ratios),
                "median_provider_to_cl100k_ratio": statistics.median(proxy_ratios),
                "mean_provider_to_cl100k_ratio": round(statistics.mean(proxy_ratios), 6),
                "max_provider_to_cl100k_ratio": max(proxy_ratios),
            }
        )
    result = {
        "schema_version": "0.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path),
        "corpus_version": manifest.get("corpus_version"),
        "config_path": str(config_path),
        "provider": provider or "dashscope",
        "model": model,
        "request_policy": "one input per request so total_tokens can be attributed to one document",
        "api_key_source": "environment" if api_key or os.environ.get("DASHSCOPE_API_KEY") else "db-gpt-config",
        "records": records,
        "summary": summary,
        "limitations": [
            "This audit measures provider usage; it is not yet a local tokenizer for recursive splitting.",
            "No embedding vectors or API keys are stored in this artifact.",
        ],
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sample-size", type=int, default=4)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = audit(args.manifest, args.config, args.model, args.sample_size)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Do not print records, response payloads, or secrets.
    print(json.dumps({"output": str(args.output), "summary": result["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
