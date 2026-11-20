"""Reproducible exploratory audit; synthetic inputs and isolated test artifacts.

Live answer tests use the saved provider credentials without printing them.
Audio replay opens no recording devices. HTTP load requires the offline preview.
Automated findings are signals for review, never semantic correctness scores.
"""

import argparse
import asyncio
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def timing(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return {}
    return {"count": len(values), "p50": round(statistics.median(values), 2),
            "p95": round(values[min(len(values) - 1, int(.95 * len(values)))], 2),
            "max": round(max(values), 2)}


def answers(args):
    from copilot.llm import LLMClient, _load_env_files
    from scripts.evaluate_answers import inspect_output, load_suite
    from interview_copilot.services.cancellation import CancellationToken
    from interview_copilot.services.corpus import CorpusIndex
    from interview_copilot.services.provider_settings import ProviderSettings

    _load_env_files()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    providers = ProviderSettings(ROOT / "data/provider_settings.db", config["llm"])
    providers.start()
    settings, credentials = providers.snapshot()
    if args.channel == "fallback":
        resolver = LLMClient(settings)
        resolver.set_credentials(credentials)
        selected = resolver._resolve_fallback()
        resolver.close()
        if selected is None:
            raise ValueError("no configured fallback provider")
        settings = {**settings, "base_url": selected["base_url"], "quick_model": selected["model"],
                    "detail_model": selected["model"], "model": selected["model"],
                    "provider_options": selected["extra"], "fallback_enabled": False}
        credentials = {"primary": selected["key"], "fallback": None}
    suites = [load_suite(ROOT / "tests/evaluation" / name) for name in
              ("answer_cases.json", "project_audit_cases.json")]
    cases = [case for suite in suites for case in suite["cases"]]
    if args.case:
        selected_ids = set(args.case)
        cases = [case for case in cases if case["id"] in selected_ids]
        if {case["id"] for case in cases} != selected_ids:
            raise ValueError("unknown case id")
    repeats = {"T02", "T06", "E01", "A01", "X01", "X02", "X09", "X11", "X19", "X28"}
    jobs = [(case, run) for case in cases for run in range(args.repetitions or (3 if case["id"] in repeats else 1))]
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "material_policy": "synthetic_only",
              "channel": args.channel,
              "parallelism": args.parallelism, "semantic_correctness": "requires review against each rubric",
              "prompt_sha256": {}, "cases": []}
    # Preload environment credentials before threads; each client owns its requests.
    from copilot.llm import QUICK_PROMPT, DETAIL_PROMPT
    report["prompt_sha256"] = {key: hashlib.sha256(value.encode()).hexdigest()
                               for key, value in (("quick", QUICK_PROMPT), ("detail", DETAIL_PROMPT))}

    def run(case, repetition):
        client, corpus = LLMClient(settings), CorpusIndex()
        client.set_credentials(credentials)
        token, contexts = CancellationToken(), []
        started = time.monotonic()
        result = {"content": "", "status": "pending", "first_content_ms": None, "attempts": [],
                  "error": "", "source_registry": []}

        def delta(text):
            if result["first_content_ms"] is None:
                result["first_content_ms"] = round((time.monotonic() - started) * 1000)
            result["content"] += text

        def attempt(number, model):
            result["content"] = ""
            result["attempts"].append({"number": number, "model": model})

        timer = threading.Timer(args.deadline, token.cancel)
        timer.daemon = True
        timer.start()
        try:
            corpus.update_documents("synthetic", case.get("references", []))
            evidence = corpus.search(case["question"], budget=4000 if case["mode"] == "detail" else 2400)
            kwargs = {"evidence": evidence, "cancel_token": token, "on_context": contexts.append,
                      "self_recent": case.get("candidate_speech", ""), "on_attempt": attempt}

            def done(text):
                result.update(content=text, status="complete")

            def error(message):
                result.update(status="error", error=message)

            if case["mode"] == "quick":
                client.stream_quick(case.get("history", []) + [{"role": "user", "content": case["question"]}],
                                    case["resume"], delta, done, error, **kwargs)
            else:
                client.stream_detail(case["question"], case.get("quick_suggestion", ""),
                                     case.get("direction", "完整版"), case.get("history", []), case["resume"],
                                     delta, done, error, **kwargs)
            if token.is_set() and result["status"] == "pending":
                result["status"] = "deadline"
            if contexts:
                pack = contexts[-1]
                result.update(source_registry=pack.sources, context_chars=pack.sent_chars,
                              context_max_chars=pack.max_chars)
        except Exception as exc:
            result.update(status="error", error=type(exc).__name__)
        finally:
            timer.cancel()
            client.close()
            corpus.close()
        result.update(duration_ms=round((time.monotonic() - started) * 1000), chars=len(result["content"]))
        row = inspect_output(case, result)
        row.update(repetition=repetition, question=case["question"])
        check, content = case.get("check", {}), result["content"]
        if any(term in content for term in check.get("forbidden", [])):
            row["automated_findings"].append("forbidden_marker")
        if check.get("required_any") and not any(term in content for term in check["required_any"]):
            row["automated_findings"].append("review_missing_expected_concept")
        # Strip credentials even from provider-generated error strings.
        encoded = json.dumps(row, ensure_ascii=False)
        for credential in credentials.values():
            if credential:
                encoded = encoded.replace(credential, "[REDACTED]")
        return json.loads(encoded)

    output_file = args.output / ("answers.json" if args.channel == "current" else "answers-fallback.json")
    with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        futures = [pool.submit(run, case, repetition) for case, repetition in jobs]
        for future in as_completed(futures):
            row = future.result()
            report["cases"].append(row)
            save(output_file, report)
            print(json.dumps({"section": "answers", "done": len(report["cases"]), "total": len(jobs),
                              "case": row["case_id"], "status": row["result"]["status"],
                              "findings": row["automated_findings"]}), flush=True)
    rows = report["cases"]
    report["summary"] = {"total": len(rows), "unique_cases": len(cases),
                         "status": dict(Counter(row["result"]["status"] for row in rows)),
                         "findings": dict(Counter(code for row in rows for code in row["automated_findings"])),
                         "first_content_ms": timing([row["result"]["first_content_ms"] for row in rows]),
                         "duration_ms": timing([row["result"]["duration_ms"] for row in rows])}
    save(output_file, report)
    print(json.dumps(report["summary"]), flush=True)


def speech(args, *, targeted=False):
    import numpy as np
    import soxr
    from copilot.hotwords import build_hotwords
    from copilot.stt import STTClient
    from interview_copilot.audio.speech_models import SpeechModels
    from interview_copilot.services.speech_pipeline import SpeechPipeline
    from scripts.validate_stt import character_error_rate

    class InlineScheduler:
        def submit(self, duration, work, **kwargs):
            future = Future()
            try:
                future.set_result(work())
            except Exception as exc:
                future.set_exception(exc)
            return future

    audio_root = args.output / "audio" if targeted else ROOT / "data/speech-v2/audio"
    def read(name, index):
        with wave.open(str(audio_root / f"{name}-{index}.wav"), "rb") as handle:
            assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (16000, 1, 2)
            return np.frombuffer(handle.readframes(handle.getnframes()), np.int16).astype(np.float32) / 32768

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    client = STTClient({**config["stt"], "base_url": config["stt"]["url"], "timeout": 90})
    models, rng = SpeechModels(ROOT / "data/speech-models"), np.random.default_rng(731)
    fixture = ROOT / "tests/fixtures/speech" / ("audit_cases.json" if targeted else "audio_cases.json")
    bases = json.loads(fixture.read_text(encoding="utf-8"))
    variants = []
    for base in bases:
        audio = np.concatenate([read(base["name"], i) if isinstance(part, str) else np.zeros(int(part * 16000), np.float32)
                                for i, part in enumerate(base["parts"])])
        kinds = ("clean", "quiet") if targeted else ("clean", "quiet", "noise", "very_quiet", "clipped", "faster", "slower", "8khz_bandwidth")
        for kind in kinds:
            data = audio.copy()
            if kind == "quiet":
                data = data * .2 + rng.normal(0, .002, len(data))
            elif kind == "noise":
                data = data + rng.normal(0, .025, len(data))
            elif kind == "very_quiet":
                data = data * .05 + rng.normal(0, .004, len(data))
            elif kind == "clipped":
                data = np.clip(data * 8, -.5, .5)
            elif kind in {"faster", "slower"}:
                data = soxr.resample(data, 16000, 12800 if kind == "faster" else 19200)
            elif kind == "8khz_bandwidth":
                data = soxr.resample(soxr.resample(data, 16000, 8000), 8000, 16000)
            variants.append({"name": base["name"] + ":" + kind, "base": base, "audio": data})
    pause = next((base for base in bases if base["name"] == "pause_incomplete"), None)
    for seconds in (() if targeted else (.3, .7, 1.4, 2, 3, 5, 7, 9, 12, 16, 24, 32)):
        data = np.concatenate([read(pause["name"], 0), np.zeros(int(seconds * 16000)), read(pause["name"], 2)])
        variants.append({"name": "pause:" + str(seconds), "base": pause, "audio": data})
    for kind in (() if targeted else ("silence", "noise", "hum", "impulses")):
        for amplitude in (.01, .05, .2):
            data = np.zeros(80000, np.float32)
            if kind == "noise":
                data = rng.normal(0, amplitude, len(data))
            elif kind == "hum":
                data = amplitude * np.sin(2 * np.pi * 180 * np.arange(len(data)) / 16000)
            elif kind == "impulses":
                for start in (4000, 16000, 33000, 47000):
                    data[start:start + 640] = rng.normal(0, amplitude, 640) * np.exp(-np.arange(640) / 150)
            variants.append({"name": kind + ":" + str(amplitude), "base": {"parts": []}, "audio": data})
    report = {"kind": "synthetic TTS and generated noise; accelerated pipeline replay, no capture devices",
              "seed": 731, "latency_note": "wall time is not end-of-speech latency", "cases": []}
    output_file = args.output / ("speech-targeted.json" if targeted else "speech.json")
    try:
        for variant in variants:
            base, events, recognitions, errors = variant["base"], [], [], []

            def transcribe(wav):
                result = client.transcribe_result(wav, prompt=build_hotwords())
                recognitions.append(result.text)
                return result

            def emit(event):
                if event["type"] == "speech_commit":
                    events.append(event)
                    pipeline.accept(event)

            pipeline = SpeechPipeline(vad=models.vad(), endpoint=models.turn, scheduler=InlineScheduler(),
                                      transcribe=transcribe, emit=emit,
                                      options={"require_question": base.get("speaker") != "self"})
            started = time.monotonic()
            try:
                pipeline.feed(np.concatenate([variant["audio"], np.zeros(16000 * 10)]).astype(np.float32))
            except Exception as exc:
                errors.append(type(exc).__name__)
            expected = " ".join(part for part in base.get("parts", []) if isinstance(part, str))
            text = " ".join(event["text"] for event in events)
            equivalents = [expected, base.get("equivalent_transcript", expected)]
            row = {"name": variant["name"], "expected": expected, "speaker": base.get("speaker", "interviewer"),
                   "audio_seconds": round(len(variant["audio"]) / 16000, 3), "commits": events,
                   "recognitions": recognitions, "errors": errors, "snapshot": pipeline.snapshot(),
                   "endpoint_probability": pipeline.turns.endpoint,
                   "wall_seconds": round(time.monotonic() - started, 3),
                   "cer": round(min(character_error_rate(value, text) for value in equivalents), 4) if expected else None,
                   "recognized_cer": round(min(character_error_rate(value, ' '.join(recognitions)) for value in equivalents), 4) if expected else None}
            row["expected_count"] = base.get("expected_count", 1 if expected else 0)
            row["expected_counts"] = base.get("expected_counts", [row["expected_count"]])
            row["count_passed"] = len(events) in row["expected_counts"] and not errors
            if base.get("expected_decision") == "no_question":
                row["count_passed"] = not errors and not any(event["decision"] == "accept_question" for event in events)
            if base.get("name") == "background":
                row["count_passed"] &= all(event["decision"] == "context_only" for event in events)
            row["coverage_passed"] = row["count_passed"] and (base.get("expected_decision") == "no_question" or row["cer"] is None or row["cer"] <= .15)
            report["cases"].append(row)
            save(output_file, report)
            print(json.dumps({"section": "speech", "done": len(report["cases"]), "total": len(variants),
                              "case": row["name"], "count_ok": row["count_passed"], "cer": row["cer"]}), flush=True)
    finally:
        client.close()
    report["summary"] = {"total": len(report["cases"]),
                         "count_passed": sum(row["count_passed"] for row in report["cases"]),
                         "coverage_passed": sum(row["coverage_passed"] for row in report["cases"]),
                         "cer": timing([row["cer"] for row in report["cases"]])}
    save(output_file, report)


def speech_targeted(args):
    speech(args, targeted=True)


def core(args):
    from scripts.evaluate_speech import VirtualClock, legacy_merger
    from interview_copilot.domain.speech import Recognition, SpeechWindow
    from interview_copilot.services.corpus import CorpusIndex
    from interview_copilot.services.speech_turns import TurnCoordinator

    report = {"seed": 731, "invariants": [], "intent": [], "retrieval": []}
    rng = random.Random(731)
    for number in range(3000):
        now = [0.0]
        turns = TurnCoordinator(clock=lambda: now[0], require_question=True, commit_quiet_ms=1400)
        count, special = rng.randint(1, 8), number % 6
        turns.activity(True, count * 16000)
        draft = turns.draft_id
        for seq in range(count):
            turns.register(SpeechWindow(seq, seq * 16000, (seq + 1) * 16000))
        order = list(range(count))
        rng.shuffle(order)
        errors = []
        for seq in order:
            turns.transcript(draft, seq, Recognition("如何避免重复扣款" if seq == 0 else "并说明幂等事务边界"))
            if turns.proposal():
                errors.append("commit_while_speaking")
        turns.activity(False, count * 16000)
        now[0] = .8
        token = turns.endpoint_request()
        if special == 1:
            turns.hold()
        elif special == 2:
            turns.gap()
        elif special == 3:
            turns.fail(draft, count - 1, "recognition_failed")
        elif special == 4:
            turns.activity(True, (count + 1) * 16000)
        elif special == 5:
            turns.clear()
        turns.endpoint_result(token, .99)
        now[0] = 2
        proposal = turns.proposal()
        if bool(proposal) != (special == 0):
            errors.append("unexpected_proposal")
        if proposal:
            if not turns.accept(proposal) or turns.accept(proposal) or turns.proposal():
                errors.append("non_unique_commit")
        report["invariants"].append({"case": number, "windows": count, "condition": special, "errors": errors})
    intent_groups = {
        "question": ["如何避免重复消费？", "介绍一下你的项目", "为什么使用 PostgreSQL", "解释虚拟内存", "请设计一个限流器", "What is idempotency?", "Can you explain retries?", "说一下 RAG 的缺点"],
        "reject": ["嗯", "好的谢谢", "咳嗽", "感谢观看", "thank you", "", "拜拜"],
        "context_only": ["我们后续还有一轮面试", "这边介绍一下总体流程", "具体面试时间会通过微信通知", "我们这边的工作时间是九点到六点", "我先给你介绍一个场景"],
        "wait": ["假设我们有一个订单系统，要求是", "如果请求超时，那么", "我们用的是 PostgreSQL 数据库"],
        "review_repetition": ["啊啊啊啊啊啊"],
        "non_question_utterance": ["请稍等一下", "请等我一会儿", "能听见我说话吗？", "声音清楚吗？", "请打开摄像头", "我喝口水，等一下", "嗯，我想一下", "我们先确认一下音频"]}
    for expected, texts in intent_groups.items():
        for text in texts:
            now = [0.0]
            turns = TurnCoordinator(clock=lambda: now[0], require_question=True, commit_quiet_ms=1400)
            turns.activity(True, 16000)
            draft = turns.register(SpeechWindow(1, 0, 16000))
            turns.transcript(draft, 1, Recognition(text))
            turns.activity(False, 16000)
            now[0] = .8
            turns.endpoint_result(turns.endpoint_request(), .99)
            now[0] = 2
            proposal = turns.proposal()
            decision = proposal["decision"] if proposal else "wait"
            passed = decision == {"question": "accept_question", "wait": "wait"}.get(expected, expected)
            if expected == "non_question_utterance":
                passed = decision != "accept_question"
            elif expected == "review_repetition":
                passed = (proposal is None and turns.snapshot()['state'] == 'needs_review'
                          and turns.snapshot()['reason'] == 'repetition')
            legacy_clock, legacy_commits = VirtualClock(), []
            merger = legacy_merger(legacy_clock, legacy_commits)
            merger.add_segment(text)
            legacy_clock.advance(14000)
            report["intent"].append({"text": text, "expected": expected, "decision": decision, "passed": passed,
                                      "state": turns.snapshot()['state'], "reason": turns.snapshot()['reason'],
                                      "legacy_commits": legacy_commits})
    corpus = CorpusIndex()
    documents = [{"id": str(i), "source": f"synthetic:doc-{i}.md", "title": f"Topic {i}",
                  "text": f"# Topic {i}\nuniquetoken{i} mechanism has atomic constraints, retry recovery and version {i}."}
                 for i in range(1000)]
    started = time.monotonic()
    corpus.update_documents("audit", documents)
    report["retrieval_index_ms"] = round((time.monotonic() - started) * 1000)
    try:
        for i in range(0, 1000, 10):
            started = time.monotonic()
            result = corpus.search(f"uniquetoken{i} atomic retry")
            report["retrieval"].append({"case": i, "expected": f"synthetic:doc-{i}.md",
                                        "first_source": result[0]["source"] if result else None,
                                        "ms": round((time.monotonic() - started) * 1000, 3),
                                        "passed": bool(result) and result[0]["source"] == f"synthetic:doc-{i}.md"})
        report["unrelated_retrieval"] = {query: len(corpus.search(query)) for query in
                                         ("stellarphotometry", "完全无关的书法问题", "what is atomic retry", "地理气候研究")}
    finally:
        corpus.close()
    trap = CorpusIndex()
    try:
        trap.update_documents("audit", [
            {"id": "name", "title": "Gephi-SK", "source": "synthetic:name.md",
             "text": "# Gephi-SK\nGephi-SK is a graph visualization tool with layout and rendering options."},
            {"id": "protocol", "title": "工具调用规范", "source": "synthetic:protocol.md",
             "text": "# 工具调用规范\nFunction Call 协议将工具的 JSON schema 传入模型，模型返回工具名和参数，应用执行工具后回传工具结果。"}])
        query = "Gephi-SK的高级封装，说明Function Call完整协议，schema怎么定义？"
        result = trap.search(query)
        report["retrieval_edge_case"] = {"query": query, "expected_source": "synthetic:protocol.md",
                                           "actual_sources": [item["source"] for item in result],
                                           "passed": any(item["source"] == "synthetic:protocol.md" for item in result)}
    finally:
        trap.close()
    report["summary"] = {"invariants": len(report["invariants"]),
                         "invariant_failures": sum(bool(row["errors"]) for row in report["invariants"]),
                         "intent": len(report["intent"]), "intent_failures": sum(not row["passed"] for row in report["intent"]),
                         "retrieval": len(report["retrieval"]), "retrieval_failures": sum(not row["passed"] for row in report["retrieval"]),
                         "retrieval_ms": timing([row["ms"] for row in report["retrieval"]])}
    save(args.output / "core.json", report)
    print(json.dumps(report["summary"]), flush=True)


def local(args):
    from scripts.validate_knowledge import QUESTIONS
    from interview_copilot.services.corpus import CorpusIndex, source_settings
    from interview_copilot.services.documents import DocumentLibrary
    from interview_copilot.services.knowledge import locate_quote

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    corpus = CorpusIndex()
    workers = type("NoBackgroundWork", (), {"cancelled": threading.Event()})()
    library = DocumentLibrary(ROOT / "data/documents", corpus, workers)
    report = {"kind": "read-only local source/quote checks; lexical retrieval diagnostic, not hybrid recall acceptance",
              "queries": [], "invalid_quotes": [], "errors": []}
    started = time.monotonic()
    try:
        corpus.sync_sources(source_settings(config, ROOT))
        library.sync_index()
        documents = {doc["id"]: doc for doc in library.list()
                     if doc["status"] == "ready" and doc["enabled"] and not doc["archived"]}
        report.update(active_documents=len(documents), corpus=corpus.stats(),
                      index_ms=round((time.monotonic() - started) * 1000))
        checked = 0
        database = (library.root / "knowledge.db").resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database, uri=True) as conn:
            for doc_id, payload in conn.execute("SELECT doc_id,payload FROM analyses WHERE status='ready'"):
                if doc_id not in documents:
                    continue
                text = library._text_path(doc_id).read_text(encoding="utf-8")
                for fact in json.loads(payload).get("facts", []):
                    checked += 1
                    if not locate_quote(text, fact["quote"], *fact["lines"]):
                        report["invalid_quotes"].append({"document_id": doc_id, "fact_id": fact["id"]})
        report["checked_quotes"] = checked
        for question in QUESTIONS:
            started = time.monotonic()
            evidence = corpus.search(question, budget=2400, limit=3)
            report["queries"].append({"question": question, "ms": round((time.monotonic() - started) * 1000, 3),
                                       "sources": [{key: item[key] for key in ("source", "kind", "lines")} for item in evidence]})
    finally:
        corpus.close()
    report["summary"] = {"active_documents": report["active_documents"], "checked_quotes": report["checked_quotes"],
                         "invalid_quotes": len(report["invalid_quotes"]), "queries": len(report["queries"]),
                         "queries_with_hits": sum(bool(row["sources"]) for row in report["queries"]),
                         "query_ms": timing([row["ms"] for row in report["queries"]])}
    save(args.output / "local.json", report)
    print(json.dumps(report["summary"]), flush=True)


async def http_load(args):
    import httpx
    report = {"kind": "offline-preview HTTP load; no real captures, provider calls or interview history", "requests": []}
    async with httpx.AsyncClient(base_url=args.preview, timeout=20, limits=httpx.Limits(max_connections=8)) as client:
        health = (await client.get("/api/health")).json()
        if health.get("preview") is not True:
            raise ValueError("HTTP load requires the synthetic preview")
        semaphore = asyncio.Semaphore(8)
        paths = ["/api/health", "/api/state", "/api/session/snapshot", "/api/history/sessions", "/api/files", "/api/providers", "/", "/files", "/settings", "/history"]

        async def get(number):
            async with semaphore:
                started = time.monotonic()
                path = paths[number % len(paths)]
                try:
                    response = await client.get(path)
                    return {"path": path, "status": response.status_code,
                            "ms": round((time.monotonic() - started) * 1000, 3), "bytes": len(response.content)}
                except Exception as exc:
                    return {"path": path, "error": type(exc).__name__, "ms": round((time.monotonic() - started) * 1000, 3)}
        report["requests"] = await asyncio.gather(*(get(number) for number in range(500)))
        report["health_after"] = (await client.get("/api/health")).json()
    report["summary"] = {"total": len(report["requests"]),
                         "errors": sum(row.get("status") != 200 for row in report["requests"]),
                         "ms": timing([row["ms"] for row in report["requests"]])}
    save(args.output / "http.json", report)
    print(json.dumps(report["summary"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sections", choices=["answers", "speech", "speech_targeted", "core", "local", "http"], nargs="+", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "data/project-audit-20261120")
    parser.add_argument("--parallelism", type=int, choices=[1, 2, 3, 4], default=2)
    parser.add_argument("--channel", choices=["current", "fallback"], default="current")
    parser.add_argument("--case", nargs="+")
    parser.add_argument("--repetitions", type=int, choices=[1, 2, 3])
    parser.add_argument("--deadline", type=float, default=45)
    parser.add_argument("--preview", default="http://127.0.0.1:8537")
    args = parser.parse_args()
    if args.deadline <= 0:
        parser.error("deadline must be positive")
    for section in args.sections:
        if section == "http":
            asyncio.run(http_load(args))
        else:
            globals()[section](args)


if __name__ == "__main__":
    main()
