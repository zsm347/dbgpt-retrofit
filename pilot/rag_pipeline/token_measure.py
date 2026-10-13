#!/usr/bin/env python3
"""Explicit length counters for chunking experiments.

The chunkers need a callable ``length_function``.  This module keeps the
length unit and tokenizer metadata together so a run cannot silently turn
character counts into token counts.  The project currently uses a DashScope
``text-embedding-v3`` candidate, whose provider tokenizer is not represented
by a local public tokenizer in this environment.  Therefore the token
backends below require an explicit tokenizer choice and never fall back to
``len(text)``.

Examples::

    # Legacy diagnostic: Python characters, not model tokens.
    pilot/.venv/bin/python pilot/rag_pipeline/token_measure.py \
        --backend characters --manifest pilot/data/eval/corpus_manifest.json

    # Only after selecting and installing a documented tokenizer:
    pilot/.venv/bin/python pilot/rag_pipeline/token_measure.py \
        --backend transformers --tokenizer-name <name-or-local-path> \
        --manifest pilot/data/eval/corpus_manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Any


class TokenizerUnavailable(RuntimeError):
    """Raised when a requested tokenizer backend cannot be constructed."""


@dataclass(frozen=True)
class LengthSpec:
    """Reproducibility metadata for one length function."""

    backend: str
    identifier: str
    unit: str
    library: str
    library_version: str | None
    add_special_tokens: bool | None = None
    special_token_policy: str | None = None
    revision: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "identifier": self.identifier,
            "unit": self.unit,
            "library": self.library,
            "library_version": self.library_version,
            "add_special_tokens": self.add_special_tokens,
            "special_token_policy": self.special_token_policy,
            "revision": self.revision,
        }


class LengthCounter:
    """Callable length counter with a serializable description."""

    def __init__(self, function: Callable[[str], int], spec: LengthSpec):
        self._function = function
        self.spec = spec

    def __call__(self, text: str) -> int:
        value = int(self._function(text))
        if value < 0:
            raise ValueError(f"Length counter returned a negative value: {value}")
        return value


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def build_length_counter(
    backend: str,
    *,
    tiktoken_encoding: str | None = None,
    tiktoken_model: str | None = None,
    tokenizer_name: str | None = None,
    revision: str | None = None,
    local_files_only: bool = False,
) -> LengthCounter:
    """Build an explicit length counter.

    ``characters`` is intentionally available only as a named legacy unit.
    ``tiktoken`` and ``transformers`` require an explicit identifier; unknown
    models never fall back to a guessed encoding.
    """

    if backend == "characters":
        return LengthCounter(
            len,
            LengthSpec(
                backend="characters",
                identifier="python-builtins.len",
                unit="python_characters",
                library="python",
                library_version=platform.python_version(),
            ),
        )

    if backend == "tiktoken":
        if not tiktoken_encoding and not tiktoken_model:
            raise ValueError(
                "tiktoken backend requires --tiktoken-encoding or "
                "--tiktoken-model; no implicit model-to-encoding fallback is allowed"
            )
        try:
            import tiktoken  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TokenizerUnavailable(
                "tiktoken is not installed in this environment; install it in the "
                "experiment environment or choose an explicitly available backend"
            ) from exc

        if tiktoken_encoding:
            try:
                encoding = tiktoken.get_encoding(tiktoken_encoding)
            except Exception as exc:  # tiktoken raises several lookup errors
                raise ValueError(
                    f"Unknown tiktoken encoding {tiktoken_encoding!r}; "
                    "pass a supported explicit encoding"
                ) from exc
            identifier = f"encoding:{tiktoken_encoding}"
        else:
            try:
                encoding = tiktoken.encoding_for_model(tiktoken_model)
            except KeyError as exc:
                raise ValueError(
                    f"tiktoken has no known encoding for model {tiktoken_model!r}; "
                    "pass --tiktoken-encoding explicitly"
                ) from exc
            identifier = f"model:{tiktoken_model}/encoding:{encoding.name}"

        def count(value: str) -> int:
            # RAG text is ordinary user text.  Treat special-token strings as
            # text rather than allowing an accidental control token.
            return len(encoding.encode(value, allowed_special=set(), disallowed_special=()))

        return LengthCounter(
            count,
            LengthSpec(
                backend="tiktoken",
                identifier=identifier,
                unit="tokens",
                library="tiktoken",
                library_version=_package_version("tiktoken"),
                add_special_tokens=False,
                special_token_policy="allowed_special=empty; disallowed_special=empty",
            ),
        )

    if backend == "transformers":
        if not tokenizer_name:
            raise ValueError(
                "transformers backend requires --tokenizer-name (model id or local path)"
            )
        try:
            from transformers import AutoTokenizer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TokenizerUnavailable(
                "transformers is not installed in this environment; install it in the "
                "experiment environment or choose an explicitly available backend"
            ) from exc
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                tokenizer_name,
                revision=revision,
                local_files_only=local_files_only,
                use_fast=True,
            )
        except Exception as exc:
            mode = "local cache" if local_files_only else "the configured model source"
            raise TokenizerUnavailable(
                f"Could not load tokenizer {tokenizer_name!r} from {mode}: {exc}"
            ) from exc

        def count(value: str) -> int:
            return len(tokenizer.encode(value, add_special_tokens=False))

        return LengthCounter(
            count,
            LengthSpec(
                backend="transformers",
                identifier=tokenizer_name,
                unit="tokens",
                library="transformers",
                library_version=_package_version("transformers"),
                add_special_tokens=False,
                special_token_policy="tokenizer.encode(add_special_tokens=False)",
                revision=revision,
            ),
        )

    raise ValueError(
        f"Unsupported length backend {backend!r}; choose characters, tiktoken, or transformers"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_documents(manifest_path: Path) -> Iterable[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for document in manifest.get("documents", []):
        if document.get("included"):
            yield document


def measure_paths(
    paths: Iterable[tuple[str, Path]], counter: LengthCounter
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for doc_id, path in paths:
        text = path.read_text(encoding="utf-8")
        records.append(
            {
                "doc_id": doc_id,
                "path": str(path),
                "content_sha256": sha256_file(path),
                "char_count": len(text),
                "length_count": counter(text),
                "length_unit": counter.spec.unit,
            }
        )
    return records


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(record["length_count"] for record in records)
    chars = sorted(record["char_count"] for record in records)
    if not values:
        return {"documents": 0, "total_length": 0, "total_chars": 0}
    return {
        "documents": len(values),
        "total_length": sum(values),
        "total_chars": sum(chars),
        "min_length": values[0],
        "p50_length": values[len(values) // 2],
        "p90_length": values[min(len(values) - 1, int(len(values) * 0.9))],
        "max_length": values[-1],
        "mean_length": round(sum(values) / len(values), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("characters", "tiktoken", "transformers"), required=True)
    parser.add_argument("--manifest", type=Path, help="Frozen corpus manifest to measure included documents")
    parser.add_argument("--input", type=Path, action="append", help="Text file; may be repeated")
    parser.add_argument("--tiktoken-encoding")
    parser.add_argument("--tiktoken-model")
    parser.add_argument("--tokenizer-name")
    parser.add_argument("--revision")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if bool(args.manifest) == bool(args.input):
        raise SystemExit("Pass exactly one of --manifest or one or more --input paths")

    try:
        counter = build_length_counter(
            args.backend,
            tiktoken_encoding=args.tiktoken_encoding,
            tiktoken_model=args.tiktoken_model,
            tokenizer_name=args.tokenizer_name,
            revision=args.revision,
            local_files_only=args.local_files_only,
        )
        if args.manifest:
            documents = list(_manifest_documents(args.manifest))
            paths = [(item["doc_id"], Path(item["canonical_path"])) for item in documents]
        else:
            paths = [(path.stem, path) for path in args.input]
        records = measure_paths(paths, counter)
    except (TokenizerUnavailable, ValueError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc

    result = {
        "schema_version": "0.1",
        "length_spec": counter.spec.as_dict(),
        "records": records,
        "summary": _summary(records),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"length_spec": result["length_spec"], **result["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
