"""Run the existing memory service against the LongMemEval JSON format.

This module is deliberately an adapter, not another memory implementation.  It
replays each benchmark question in an isolated scope, writes the haystack in
chronological session order, searches with the question, and emits both the
official prediction JSONL shape and a provenance-rich run log.

The official LongMemEval scorer is intentionally not copied here.  Predictions
can be passed to the pinned scorer after the runner has been smoke-tested.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "examples" / "agent_memory_v1"))

from memory_v1 import MemoryScope, MemoryService, SourceMessage, build_answer_prompt  # noqa: E402
from memory_v1.store import MemoryStoreConfig, create_chat_model  # noqa: E402


def _non_empty_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _message_text(value: Any, *, field: str) -> str:
    """Read the text variants used by released LongMemEval files."""

    if isinstance(value, str):
        return _non_empty_text(value, field=field)
    if isinstance(value, Mapping):
        if "content" in value:
            return _message_text(value["content"], field=field)
        if "text" in value:
            return _message_text(value["text"], field=field)
        raise ValueError(f"{field} mapping has no content/text")
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts: list[str] = []
        for index, part in enumerate(value):
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, Mapping) and isinstance(part.get("content"), str):
                parts.append(part["content"])
            else:
                raise ValueError(f"{field}[{index}] is not a text block")
        return _non_empty_text("".join(parts), field=field)
    raise ValueError(f"{field} is not text")


def parse_timestamp(value: Any, *, field: str) -> datetime:
    """Parse a benchmark timestamp without inventing a local timezone.

    LongMemEval releases contain ISO timestamps, but accepting a date-only value
    makes the adapter robust to hand-curated subsets.  A naive value is treated
    as UTC and recorded in the run manifest as an adapter normalization.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, dt_time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must not be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            # The released LongMemEval files use strings such as
            # ``2023/05/30 (Tue) 23:40``.  The weekday is presentation-only;
            # parse the date/time portion while retaining the same UTC
            # normalization used for ISO values.
            match = re.fullmatch(
                r"(?P<date>\d{4}/\d{2}/\d{2})\s+\([A-Za-z]{3}\)\s+"
                r"(?P<time>\d{2}:\d{2}(?::\d{2})?)",
                text,
            )
            if not match:
                raise ValueError(f"{field} is not ISO date/time: {value!r}") from exc
            parsed = datetime.fromisoformat(
                f"{match.group('date').replace('/', '-')}T{match.group('time')}"
            )
    else:
        raise ValueError(f"{field} is not a supported date/time value")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _role(value: Any, *, field: str) -> str:
    role = _non_empty_text(value, field=field).lower()
    aliases = {"human": "user", "ai": "assistant", "model": "assistant"}
    role = aliases.get(role, role)
    if role not in {"user", "assistant", "tool", "system"}:
        raise ValueError(f"{field} has unsupported role {role!r}")
    return role


@dataclass(frozen=True)
class BenchmarkSession:
    session_id: str
    observed_at: datetime
    messages: tuple[SourceMessage, ...]


@dataclass(frozen=True)
class BenchmarkItem:
    question_id: str
    question_type: str
    question: str
    reference_answer: Any
    question_date: datetime
    sessions: tuple[BenchmarkSession, ...]
    answer_session_ids: tuple[str, ...]


def _raw_session_messages(session: Any) -> Sequence[Any]:
    if isinstance(session, Mapping):
        for key in ("messages", "turns", "conversation"):
            if key in session:
                value = session[key]
                if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                    raise ValueError(f"session.{key} must be a message list")
                return value
        raise ValueError("session mapping has no messages/turns/conversation")
    if not isinstance(session, Sequence) or isinstance(session, (str, bytes)):
        raise ValueError("haystack session must be a message list")
    return session


def _source_message(
    *, question_id: str, session_id: str, session_index: int,
    message_index: int, raw: Any, observed_at: datetime
) -> SourceMessage | None:
    if isinstance(raw, Mapping):
        role_value = raw.get("role", raw.get("speaker"))
        content_value = raw.get("content", raw.get("text"))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) == 2:
        role_value, content_value = raw
    else:
        raise ValueError(f"message {question_id}/{session_id}/{message_index} has unsupported shape")
    role = _role(role_value, field=f"message {question_id}/{session_id}/{message_index}.role")
    try:
        content = _message_text(
            content_value,
            field=f"message {question_id}/{session_id}/{message_index}.content",
        )
    except ValueError as exc:
        # The cleaned release retains a small number of empty ShareGPT turns.
        # They carry no evidence and cannot be represented by SourceMessage;
        # omit only these blank turns and keep malformed non-empty messages as
        # hard errors.
        if "must be a non-empty string" not in str(exc):
            raise
        return None
    return SourceMessage(
        # Some released items repeat a haystack_session_id at different dates.
        # Keep the benchmark's session ID as the first path component for
        # provenance scoring, while using its occurrence index to make each
        # source message globally unique within the item.
        id=f"{question_id}/{session_id}/occurrence-{session_index}/message-{message_index}",
        role=role,
        content=content,
        observed_at=observed_at,
    )


def parse_item(raw: Mapping[str, Any]) -> BenchmarkItem:
    question_id = _non_empty_text(raw.get("question_id"), field="question_id")
    question_type = _non_empty_text(raw.get("question_type", "unknown"), field="question_type")
    question = _non_empty_text(raw.get("question"), field=f"{question_id}.question")
    question_date = parse_timestamp(raw.get("question_date"), field=f"{question_id}.question_date")
    raw_ids = raw.get("haystack_session_ids")
    raw_dates = raw.get("haystack_dates")
    raw_sessions = raw.get("haystack_sessions")
    if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)):
        raise ValueError(f"{question_id}.haystack_session_ids must be a list")
    if not isinstance(raw_dates, Sequence) or isinstance(raw_dates, (str, bytes)):
        raise ValueError(f"{question_id}.haystack_dates must be a list")
    if not isinstance(raw_sessions, Sequence) or isinstance(raw_sessions, (str, bytes)):
        raise ValueError(f"{question_id}.haystack_sessions must be a list")
    if not (len(raw_ids) == len(raw_dates) == len(raw_sessions)):
        raise ValueError(f"{question_id} session IDs/dates/messages have different lengths")

    sessions: list[BenchmarkSession] = []
    for session_index, (raw_id, raw_date, raw_session) in enumerate(
        zip(raw_ids, raw_dates, raw_sessions)
    ):
        session_id = _non_empty_text(raw_id, field=f"{question_id}.haystack_session_ids[{session_index}]")
        observed_at = parse_timestamp(raw_date, field=f"{question_id}.haystack_dates[{session_index}]")
        messages = tuple(
            message
            for message in (
                _source_message(
                question_id=question_id,
                session_id=session_id,
                session_index=session_index,
                message_index=message_index,
                raw=message,
                observed_at=observed_at,
            )
                for message_index, message in enumerate(_raw_session_messages(raw_session))
            )
            if message is not None
        )
        if not messages:
            raise ValueError(f"{question_id}/{session_id} contains no messages")
        sessions.append(BenchmarkSession(session_id, observed_at, messages))

    answers = raw.get("answer_session_ids", [])
    if not isinstance(answers, Sequence) or isinstance(answers, (str, bytes)):
        raise ValueError(f"{question_id}.answer_session_ids must be a list")
    return BenchmarkItem(
        question_id=question_id,
        question_type=question_type,
        question=question,
        reference_answer=raw.get("answer"),
        question_date=question_date,
        sessions=tuple(sessions),
        answer_session_ids=tuple(str(value) for value in answers),
    )


def load_items(path: Path) -> list[BenchmarkItem]:
    value = json.loads(path.read_text())
    if isinstance(value, Mapping):
        for key in ("data", "examples", "items"):
            if key in value:
                value = value[key]
                break
    if not isinstance(value, list):
        raise ValueError("LongMemEval file must contain a JSON list")
    return [parse_item(item) for item in value if isinstance(item, Mapping)]


def _slug(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    digest = hashlib.sha256(value.encode()).hexdigest()[:10]
    return f"{safe[:48] or 'item'}-{digest}"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _answer_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts).strip()
    return str(content).strip()


def _profile_overrides(profile: Path | None) -> dict[str, Any]:
    if profile is None:
        return {}
    import tomllib

    config = tomllib.loads(profile.expanduser().read_text())
    llms = config.get("models", {}).get("llms", [])
    if not llms:
        raise ValueError(f"profile {profile} has no models.llms entry")
    llm = llms[0]
    overrides: dict[str, Any] = {
        "api_key": llm.get("api_key"),
        "base_url": llm.get("api_base"),
        "chat_model": llm.get("name"),
    }
    embeddings = config.get("models", {}).get("embeddings", [])
    if embeddings:
        overrides["embedding_model"] = embeddings[0].get("name")
    return {key: value for key, value in overrides.items() if value is not None}


def _versions() -> dict[str, str | None]:
    names = ("mem0ai", "langmem", "langchain-openai", "qdrant-client")
    result: dict[str, str | None] = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _candidate_record(record: Any, *, question_id: str) -> dict[str, Any]:
    value = _jsonable(record)
    source_refs = value.get("source_refs", []) if isinstance(value, Mapping) else []
    prefix = f"{question_id}/"
    source_session_ids = sorted({
        source_id[len(prefix):].split("/", 1)[0]
        for ref in source_refs
        if isinstance(ref, Mapping)
        and isinstance(ref.get("id"), str)
        and (source_id := ref["id"]).startswith(prefix)
        and "/" in source_id[len(prefix):]
    })
    return {"memory": value, "source_session_ids": source_session_ids}


def _session_provenance(
    candidate_records: Sequence[Mapping[str, Any]], gold_session_ids: Sequence[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project memory provenance to session IDs for diagnostic retrieval stats.

    This is deliberately labelled provenance rather than evidence recall: a
    consolidated memory may cite a session while its text no longer contains
    every answer-bearing detail from that session.  Official LongMemEval
    retrieval scoring should still be run on a separately reviewed projection.
    """

    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, candidate in enumerate(candidate_records):
        memory = candidate.get("memory", {})
        for session_id in candidate.get("source_session_ids", []):
            if session_id in seen:
                continue
            seen.add(session_id)
            ranked.append(
                {
                    "corpus_id": session_id,
                    "text": memory.get("content", {}).get("content", "")
                    if isinstance(memory, Mapping)
                    else "",
                    "memory_id": memory.get("id") if isinstance(memory, Mapping) else None,
                    "memory_rank": rank + 1,
                }
            )
    gold = set(gold_session_ids)
    metrics: dict[str, Any] = {"gold_count": len(gold), "defined": bool(gold)}
    if gold:
        for cutoff in (1, 3, 5, 10):
            selected = {item["corpus_id"] for item in ranked[:cutoff]}
            metrics[f"recall_any@{cutoff}"] = bool(selected & gold)
            metrics[f"recall_all@{cutoff}"] = gold.issubset(selected)
    return ranked, metrics


async def run_item(
    item: BenchmarkItem,
    *,
    output_dir: Path,
    config_overrides: Mapping[str, Any],
    answer_top_k: int,
    skip_answer: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay one item and return the rich record plus official prediction."""

    item_dir = output_dir / "data" / _slug(item.question_id)
    config = MemoryStoreConfig.from_env(
        **dict(config_overrides),
        data_dir=item_dir,
        qdrant_path=item_dir / "qdrant",
        history_db_path=item_dir / "history.db",
        collection_name=f"longmemeval_{_slug(item.question_id).replace('-', '_')}",
    )
    service = MemoryService.from_config(config)
    answer_model = None if skip_answer else create_chat_model(config)
    scope = MemoryScope(scope_type="user", scope_id=f"longmemeval:{item.question_id}")
    recent: list[SourceMessage] = []
    writes: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    for session_index, session in enumerate(item.sessions):
        record: dict[str, Any] = {
            "session_index": session_index,
            "session_id": session.session_id,
            "observed_at": session.observed_at.isoformat(),
            "message_ids": [message.id for message in session.messages],
            "message_roles": [message.role for message in session.messages],
        }
        try:
            summary = await service.add(
                scope,
                session.messages,
                recent,
                batch_id=f"longmemeval-{_slug(item.question_id)}-{session_index}",
            )
            record["summary"] = summary.model_dump(mode="json")
            known_ids.update(summary.added_ids)
            known_ids.update(summary.updated_ids)
        except Exception as exc:  # preserve failed cases for denominator accounting
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            summary = getattr(exc, "summary", None)
            if summary is not None:
                record["partial_summary"] = _jsonable(summary)
                known_ids.update(summary.added_ids)
                known_ids.update(summary.updated_ids)
        writes.append(record)
        recent.extend(session.messages)

    retrieval: dict[str, Any] = {"query": item.question, "ranked_items": [], "error": None}
    answer = ""
    answer_error: dict[str, str] | None = None
    try:
        candidates = await service.search(scope, item.question, top_k=answer_top_k)
        candidate_records = [
            _candidate_record(candidate, question_id=item.question_id)
            for candidate in candidates
        ]
        retrieval["ranked_items"] = candidate_records
        session_ranked, provenance_metrics = _session_provenance(
            candidate_records, item.answer_session_ids
        )
        retrieval["session_ranked_items"] = session_ranked
        retrieval["provenance_metrics"] = provenance_metrics
        if not skip_answer:
            response = await answer_model.ainvoke(  # type: ignore[union-attr]
                build_answer_prompt(item.question, candidates, reference_time=item.question_date)
            )
            answer = _answer_text(response)
            retrieval["answer_usage"] = _jsonable(getattr(response, "usage_metadata", None))
    except Exception as exc:
        answer_error = {"type": type(exc).__name__, "message": str(exc)}
        retrieval["error"] = answer_error

    rich = {
        "question_id": item.question_id,
        "question_type": item.question_type,
        "question": item.question,
        "question_date": item.question_date.isoformat(),
        "reference_answer": item.reference_answer,
        "answer_session_ids": list(item.answer_session_ids),
        "writes": writes,
        "retrieval": retrieval,
        "hypothesis": answer,
        "answer_error": answer_error,
        "known_memory_ids": sorted(known_ids),
    }
    prediction = {"question_id": item.question_id, "hypothesis": answer}
    return rich, prediction


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def _dry_run(items: Sequence[BenchmarkItem], output_dir: Path, dataset_path: Path) -> None:
    rows = []
    for item in items:
        rows.append(
            {
                "question_id": item.question_id,
                "question_type": item.question_type,
                "session_count": len(item.sessions),
                "message_count": sum(len(session.messages) for session in item.sessions),
                "answer_session_ids": list(item.answer_session_ids),
            }
        )
    (output_dir / "dry_run.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(json.dumps({"status": "dry_run", "items": len(rows), "output": str(output_dir / "dry_run.json")}, ensure_ascii=False))


async def run(args: argparse.Namespace) -> int:
    dataset_path = args.dataset.expanduser().resolve()
    items = load_items(dataset_path)
    if args.question_id:
        wanted = set(args.question_id)
        items = [item for item in items if item.question_id in wanted]
    else:
        items = items[args.offset : args.offset + args.limit if args.limit else None]
    if not items:
        raise ValueError("no LongMemEval items selected")

    output_dir = args.output.expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        _dry_run(items, output_dir, dataset_path)
        return 0

    started = datetime.now(timezone.utc)
    config_overrides = _profile_overrides(args.profile)
    manifest = {
        "run_id": args.run_id,
        "started_at": started.isoformat(),
        "dataset_path": str(dataset_path),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "selected_count": len(items),
        "answer_top_k": args.answer_top_k,
        "skip_answer": args.skip_answer,
        "config": {
            "chat_model": config_overrides.get("chat_model", os.getenv("AGENT_MEMORY_CHAT_MODEL", "deepseek-v4.1-flash")),
            "embedding_model": config_overrides.get("embedding_model", os.getenv("AGENT_MEMORY_EMBEDDING_MODEL", "text-embedding-v3")),
            "embedding_dims": int(os.getenv("AGENT_MEMORY_EMBEDDING_DIMS", "1024")),
        },
        "versions": _versions(),
        "status": "running",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    rich_records: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    errors = 0
    for index, item in enumerate(items, start=1):
        print(json.dumps({"status": "item_start", "index": index, "total": len(items), "question_id": item.question_id}, ensure_ascii=False), flush=True)
        try:
            rich, prediction = await run_item(
                item,
                output_dir=output_dir,
                config_overrides=config_overrides,
                answer_top_k=args.answer_top_k,
                skip_answer=args.skip_answer,
            )
        except Exception as exc:
            errors += 1
            rich = {
                "question_id": item.question_id,
                "question_type": item.question_type,
                "question": item.question,
                "reference_answer": item.reference_answer,
                "hypothesis": "",
                "run_error": {"type": type(exc).__name__, "message": str(exc)},
            }
            prediction = {"question_id": item.question_id, "hypothesis": ""}
        rich_records.append(rich)
        predictions.append(prediction)
        _write_jsonl(output_dir / "records.jsonl", rich_records)
        _write_jsonl(output_dir / "predictions.jsonl", predictions)
        print(json.dumps({"status": "item_done", "index": index, "total": len(items), "question_id": item.question_id, "error": "run_error" in rich}, ensure_ascii=False), flush=True)

    manifest.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "completed" if errors == 0 else "completed_with_errors",
            "run_errors": errors,
            "predictions_path": str(output_dir / "predictions.jsonl"),
            "records_path": str(output_dir / "records.jsonl"),
        }
    )
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if errors == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="LongMemEval cleaned JSON file")
    parser.add_argument("--output", type=Path, required=True, help="empty output directory")
    parser.add_argument("--run-id", default="longmemeval-s-adapter-v0")
    parser.add_argument("--profile", type=Path, help="optional DB-GPT compatible TOML profile")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--question-id", action="append")
    parser.add_argument("--answer-top-k", type=int, default=10)
    parser.add_argument("--skip-answer", action="store_true", help="retrieval-only; do not call the answer model")
    parser.add_argument("--dry-run", action="store_true", help="validate and summarize input without API calls")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.offset < 0 or (args.limit is not None and args.limit < 1):
        raise SystemExit("--offset must be non-negative and --limit must be positive")
    if args.answer_top_k < 1:
        raise SystemExit("--answer-top-k must be positive")
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
