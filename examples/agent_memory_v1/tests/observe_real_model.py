"""Opt-in real-model observations on synthetic fixtures and a fresh temporary store.

This is a manual runner, not an automatically discovered unit test.
"""
import asyncio
import hashlib
import importlib.metadata
import json
import os
import argparse
import tempfile
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

os.environ["MEM0_TELEMETRY"] = "false"
os.environ["ANONYMIZED_TELEMETRY"] = "false"
REPO = Path(__file__).resolve().parents[3]
OUT = None
sys.path.insert(0, str(REPO / "examples/agent_memory_v1"))

from langchain_core.callbacks import BaseCallbackHandler

from memory_v1 import MemoryScope, MemoryService, SourceMessage, build_answer_prompt
from memory_v1.store import MemoryStore, MemoryStoreConfig, create_chat_model
from memory_v1.manager import create_memory_manager
from memory_v1.prompts import build_write_query

CASES = json.loads((Path(__file__).parent / "fixtures/multi_type.json").read_text())

SECRET = ""
EVENTS = []
START = time.monotonic()
RESULT = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "reference_time": datetime.now(timezone.utc).isoformat(),
    "versions": {n: importlib.metadata.version(n) for n in ("mem0ai", "langmem", "langchain-openai", "qdrant-client")},
    "models": {"chat": "deepseek-v4.1-flash", "embedding": "text-embedding-v3", "dimensions": 1024},
    "cases": {}, "events_file": None,
}

def safe_error(exc):
    s = str(exc)
    return {"type": type(exc).__name__, "message": s.replace(SECRET, "[redacted]") if SECRET else s}

def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)

def save():
    (OUT / "results.json").write_text(jdump(RESULT))

def emit(kind, case, **values):
    event = {"elapsed_s": round(time.monotonic()-START, 1), "kind": kind, "case": case, **values}
    EVENTS.append(event)
    with (OUT / "events.jsonl").open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    short = {k: v for k, v in values.items() if k in ("turn", "question", "added", "updated", "skipped", "records", "error", "status")}
    print(json.dumps({"elapsed_s": event["elapsed_s"], "kind": kind, "case": case, **short}, ensure_ascii=False), flush=True)

def fingerprint():
    files = sorted(set((REPO / "examples/agent_memory_v1").rglob("*.py")) |
                   set((REPO / "examples/agent_memory_v1").glob("*.md")) |
                   set((REPO / "examples/agent_memory_v1").glob("requirements*")) |
                   set((REPO / "docs").glob("agent_long_term_memory*.md")))
    return {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

class ToolRecorder(BaseCallbackHandler):
    def __init__(self, record): self.record = record
    def on_llm_end(self, response, **kwargs):
        for generations in response.generations:
            for generation in generations:
                msg = getattr(generation, "message", None)
                calls = getattr(msg, "tool_calls", [])
                safe_calls = [{"name": c["name"], "args": {k:v for k,v in c["args"].items() if k != "planned_edits"}} for c in calls]
                self.record.setdefault("model_tools", []).append(safe_calls)

class RecordingManager:
    def __init__(self, real, case_data):
        self.real, self.case_data, self.step = real, case_data, None
    async def ainvoke(self, payload):
        record = {
            "turn": self.step, "existing": payload["existing"],
            "message": payload["messages"][0].content, "max_steps": payload["max_steps"],
            "new_source_ids": payload.get("new_source_ids"),
        }
        self.case_data["manager_calls"].append(record)
        try:
            raw = await self.real.ainvoke(payload, config={"callbacks": [ToolRecorder(record)]})
            record["result"] = [{"id": x.id, "content": (x.content.model_dump(mode="json") if hasattr(x.content, "model_dump") else x.content)} for x in raw]
            save()
            return raw
        except Exception as exc:
            record["error"] = safe_error(exc)
            save()
            raise

async def get_snapshot(service, scope, ids):
    snapshot = []
    for mid in sorted(ids):
        record = await service.get(scope, mid)
        if record:
            snapshot.append(record.model_dump(mode="json"))
    return snapshot

async def run_case(case, store, answer_model, sem):
    async with sem:
        cid = case["id"]
        data = {"description": case["description"], "writes": [], "manager_calls": [], "answers": []}
        RESULT["cases"][cid] = data
        service = MemoryService(store, RecordingManager(create_memory_manager(create_chat_model(store.config)), data))
        scope = MemoryScope(scope_type="project" if cid != "A_preferences" else "user", scope_id=cid)
        known = set()
        recent = []
        emit("case_start", cid)
        async def answer(item, stage):
            ad = {"stage": stage, **item}
            data["answers"].append(ad)
            try:
                candidates = await asyncio.wait_for(service.search(scope, item["question"]), timeout=90)
                ad["candidates"] = [r.model_dump(mode="json") for r in candidates]
                response = await asyncio.wait_for(answer_model.ainvoke(build_answer_prompt(item["question"], candidates, reference_time=datetime.fromisoformat(RESULT["reference_time"]))), timeout=120)
                ad["answer"] = response.content
                ad["usage"] = response.usage_metadata
                emit("answer_done", cid, question=item["question"], records=len(candidates))
            except Exception as exc:
                ad["error"] = safe_error(exc)
                emit("answer_error", cid, question=item["question"], error=ad["error"])
            save()

        for i, entry in enumerate(case["turns"]):
            messages = [SourceMessage(id=f"{cid}/r{i+1}/{role}{j}", role=role, content=text, observed_at=datetime.fromisoformat(entry["time"])) for j, (role, text) in enumerate(entry["messages"])]
            step = {"turn": i+1, "input": [m.model_dump(mode="json") for m in messages], "recent_source_ids": [m.id for m in recent[-10:]], "write_query": build_write_query(messages)}
            data["writes"].append(step)
            service.manager.step = i+1
            emit("write_start", cid, turn=i+1)
            try:
                summary = await asyncio.wait_for(service.add(scope, messages, recent, batch_id=f"observe-{cid}-{i+1}"), timeout=180)
                step["summary"] = summary.model_dump(mode="json")
                known.update(summary.added_ids)
                known.update(summary.updated_ids)
                step["snapshot"] = await get_snapshot(service, scope, known)
                emit("write_done", cid, turn=i+1, added=len(summary.added_ids), updated=len(summary.updated_ids), skipped=len(summary.skipped_ids), records=len(step["snapshot"]))
            except Exception as exc:
                step["error"] = safe_error(exc)
                if hasattr(exc, "summary"):
                    step["partial_summary"] = exc.summary.model_dump(mode="json")
                    known.update(exc.summary.added_ids)
                    known.update(exc.summary.updated_ids)
                step["snapshot"] = await get_snapshot(service, scope, known)
                emit("write_error", cid, turn=i+1, error=step["error"])
            recent.extend(messages)
            save()
            for item in case.get("intermediate", {}).get(str(i), []):
                await answer(item, f"after_turn_{i+1}")
        for item in case["queries"]:
            await answer(item, "final")
        data["final_snapshot"] = await get_snapshot(service, scope, known)
        emit("case_done", cid, records=len(data["final_snapshot"]))
        save()

async def main():
    global SECRET, OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, help="Optional DB-GPT OpenAI-compatible TOML profile; otherwise use documented environment variables.")
    parser.add_argument("--output", type=Path, help="New output directory outside the repository; default creates a unique temporary directory.")
    parser.add_argument("--cases", nargs="+", choices=[c["id"] for c in CASES])
    parser.add_argument("--reference-time", help="Timezone-aware answer reference timestamp; default is current UTC time.")
    parser.add_argument("--targeted-repeat", action="store_true", help="Repeat selected cases in new scopes; use two key questions for D/F.")
    args = parser.parse_args()
    if args.output:
        OUT = args.output.expanduser().resolve()
        if OUT.is_relative_to(REPO):
            parser.error("--output must be outside the repository")
        OUT.mkdir(parents=True, exist_ok=False)
    else:
        OUT = Path(tempfile.mkdtemp(prefix="dbgpt-memory-observation-"))
    reference = datetime.fromisoformat(args.reference_time) if args.reference_time else datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        parser.error("--reference-time must include a timezone")
    RESULT["reference_time"] = reference.isoformat()
    RESULT["events_file"] = str(OUT / "events.jsonl")
    overrides = {}
    if args.profile:
        profile = tomllib.loads(args.profile.expanduser().read_text())
        llm = profile["models"]["llms"][0]
        embeddings = profile["models"].get("embeddings", [])
        overrides = {"api_key": llm["api_key"], "base_url": llm["api_base"], "chat_model": llm["name"]}
        if embeddings:
            overrides["embedding_model"] = embeddings[0]["name"]
    config = MemoryStoreConfig.from_env(
        **overrides, data_dir=OUT / "data", qdrant_path=OUT / "data/qdrant",
        history_db_path=OUT / "data/history.db", collection_name="synthetic_memory_observation",
    )
    SECRET = config.require_api_key()
    RESULT["models"] = {"chat": config.chat_model, "embedding": config.embedding_model, "dimensions": config.embedding_dims}
    RESULT["source_fingerprint_before"] = fingerprint()
    cases = [case for case in CASES if not args.cases or case["id"] in args.cases]
    if args.targeted_repeat:
        for case in cases:
            if case["id"].startswith(("D_", "F_")):
                case["queries"] = case["queries"][:2]
            case["id"] += "_repeat"
    (OUT / "fixtures.json").write_text(jdump(cases))
    save()
    store = MemoryStore.from_config(config)
    try:
        model = create_chat_model(config)
        sem = asyncio.Semaphore(2)
        await asyncio.gather(*(run_case(case, store, model, sem) for case in cases))
        foreign_scope = MemoryScope(scope_type="project", scope_id="empty_scope")
        foreign = await store.search(foreign_scope, "Atlas PostgreSQL")
        RESULT["scope_checks"] = {"empty_scope_results": len(foreign)}
        stored_ids = [record["id"] for case in RESULT["cases"].values() for record in case.get("final_snapshot", [])]
        if stored_ids:
            try:
                await store.get(foreign_scope, stored_ids[0])
                RESULT["scope_checks"]["foreign_get"] = "unexpected success"
            except PermissionError as exc:
                RESULT["scope_checks"]["foreign_get"] = "rejected"
                RESULT["scope_checks"]["foreign_get_error"] = safe_error(exc)
        RESULT["source_fingerprint_after"] = fingerprint()
        RESULT["source_unchanged"] = RESULT["source_fingerprint_before"] == RESULT["source_fingerprint_after"]
        RESULT["finished_at"] = datetime.now(timezone.utc).isoformat()
        RESULT["elapsed_s"] = round(time.monotonic() - START, 1)
        save()
        emit("run_done", "all", status="complete")
        print("RESULT_FILE " + str(OUT / "results.json"), flush=True)
    finally:
        store.memory.vector_store.client.close()


if __name__ == "__main__":
    asyncio.run(main())
