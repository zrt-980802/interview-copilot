"""Opt-in live smoke test using synthetic material and process-only credentials."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from copilot.llm import DETAIL_PROMPT, LLMClient, QUICK_PROMPT
from interview_copilot.services.cancellation import CancellationToken
from interview_copilot.services.corpus import CorpusIndex


CASES = {
    "principle": "Redis 缓存和数据库怎么保持一致？请说明更新和删除的顺序、并发风险及一致性边界。",
    "missing-evidence": "你主导的订单系统做了哪些性能优化？具体提升了多少？",
    "detail": "Redis 缓存和数据库怎么保持一致？",
}
SYNTHETIC_RESUME = (
    "以下是合成测试材料，不对应真实个人：参与一个练习用订单查询接口，使用 Python 和 PostgreSQL。"
    "角色为接口开发，未承担负责人职责。没有生产环境性能压测，没有量化性能指标，没有论文。"
)


async def diagnose(args, credential):
    started = time.monotonic()
    result = {"case_id": "protocol-diagnostic", "model": args.model, "status": "pending",
              "http_status": None, "headers_ms": None, "first_frame_ms": None,
              "first_content_ms": None, "frames": 0, "reasoning_chars": 0,
              "content_chars": 0, "finish_reason": None, "usage": {}}
    try:
        async with asyncio.timeout(args.deadline_seconds):
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                async with client.stream("POST", args.base_url.rstrip("/") + "/chat/completions",
                                         headers={"Authorization": "Bearer " + credential},
                                         json={"model": args.model, "stream": True,
                                               "max_tokens": args.max_tokens,
                                               **({"thinking": {"type": "disabled"}} if args.disable_thinking else {}),
                                               "messages": [{"role": "user", "content": "只回复 OK，不要解释。"}]}) as response:
                    result.update(http_status=response.status_code,
                                  headers_ms=round((time.monotonic() - started) * 1000))
                    if response.status_code != 200:
                        result["status"] = "http_error"
                    else:
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            result["frames"] += 1
                            if result["first_frame_ms"] is None:
                                result["first_frame_ms"] = round((time.monotonic() - started) * 1000)
                            if line[5:].strip() == "[DONE]":
                                result["status"] = "complete"
                                break
                            try:
                                payload = json.loads(line[5:])
                                if payload.get("usage"):
                                    result["usage"] = {key: value for key, value in payload["usage"].items()
                                                       if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(value, int)}
                                choice = (payload.get("choices") or [{}])[0]
                                delta = choice.get("delta") or {}
                                result["reasoning_chars"] += len(delta.get("reasoning_content") or "")
                                content = delta.get("content") or ""
                                result["content_chars"] += len(content)
                                if content and result["first_content_ms"] is None:
                                    result["first_content_ms"] = round((time.monotonic() - started) * 1000)
                                if choice.get("finish_reason"):
                                    result["finish_reason"] = str(choice["finish_reason"])[:32]
                            except (json.JSONDecodeError, TypeError, AttributeError, IndexError):
                                continue
                        if result["status"] == "pending":
                            result["status"] = "eof"
    except TimeoutError:
        result["status"] = "deadline"
    except Exception as exc:
        result.update(status="error", error=type(exc).__name__)
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result


def probe(args, credential):
    case = getattr(args, "case_data", None) or {
        "id": args.case, "mode": "detail" if args.case == "detail" else "quick",
        "question": CASES[args.case], "resume": SYNTHETIC_RESUME,
        "direction": "并发风险与一致性边界",
        "quick_suggestion": "建议：先更新数据库，再删除缓存，配合失效重试与监控。"}
    key_name = "INTERVIEW_COPILOT_LIVE_PROBE_KEY"
    os.environ[key_name] = credential
    config = {"base_url": args.base_url.rstrip("/"), "model": args.model,
              "quick_model": args.model, "detail_model": args.model,
              "api_key_env": [key_name], "temperature": 0.4, "max_tokens": args.max_tokens}
    if getattr(args, "disable_thinking", False):
        config["provider_options"] = {"thinking": {"type": "disabled"}}
    elif getattr(args, "enable_thinking", False):
        config["provider_options"] = {"thinking": {"type": "enabled"}}
    prompts = getattr(args, "prompts", None) or {"quick": QUICK_PROMPT, "detail": DETAIL_PROMPT}
    token = CancellationToken()
    started = time.monotonic()
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "case_id": args.case,
              "model": args.model, "temperature": 0.4, "max_tokens": args.max_tokens,
              "prompt_sha256": hashlib.sha256(prompts[case["mode"]].encode("utf-8")).hexdigest(),
              "case_sha256": hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
              "deadline_s": args.deadline_seconds, "first_content_ms": None,
              "provider_options": config.get("provider_options", {}),
              "attempts": [], "content": "", "status": "pending", "error": ""}

    def delta(text):
        if result["first_content_ms"] is None:
            result["first_content_ms"] = round((time.monotonic() - started) * 1000)
        result["content"] += text

    def done(text):
        result.update(content=text, status="complete")

    def error(message):
        result.update(error=message, status="error")

    def attempt(number, model):
        result["attempts"].append({"attempt": number, "model": model})
        result["content"] = ""

    def context(pack):
        result["context"] = {"original_chars": pack.original_chars, "sent_chars": pack.sent_chars,
                             "max_chars": pack.max_chars, "partial_fields": list(pack.excerpted),
                             "messages_sha256": hashlib.sha256(json.dumps(pack.messages, ensure_ascii=False).encode()).hexdigest()}
        result["retrieved_evidence"] = [{key: item[key] for key in ("id", "source", "kind", "lines")}
                                         for item in pack.evidence]
        result["source_registry"] = pack.sources

    deadline = threading.Timer(args.deadline_seconds, token.cancel)
    deadline.daemon = True
    deadline.start()
    client = None
    corpus = None
    try:
        client = LLMClient(config, prompts=prompts)
        corpus = CorpusIndex()
        corpus.update_documents("synthetic", case.get("references", []))
        evidence = corpus.search(case["question"] + " " + case.get("direction", ""),
                                 budget=4000 if case["mode"] == "detail" else 2400)
        result["retrieved_evidence"] = [{key: item[key] for key in ("id", "source", "kind", "lines")}
                                         for item in evidence]
        kwargs = {"cancel_token": token, "on_attempt": attempt,
                  "jd": "后端开发，关注正确性、故障处理与设计权衡。", "evidence": evidence,
                  "on_context": context,
                  "self_recent": case.get("candidate_speech", "")}
        if case["mode"] == "detail":
            client.stream_detail(case["question"], case.get("quick_suggestion", ""),
                                 case.get("direction", "完整版"), case.get("history", []), case["resume"],
                                 delta, done, error, **kwargs)
        else:
            client.stream_quick(case.get("history", []) + [{"role": "user", "content": case["question"]}],
                                case["resume"], delta, done, error, **kwargs)
        if token.is_set() and result["status"] == "pending":
            result["status"] = "deadline"
    except Exception as exc:
        result.update(status="error", error=type(exc).__name__)
    finally:
        deadline.cancel()
        if corpus is not None:
            corpus.close()
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                result.update(status="error", error=type(exc).__name__)
        os.environ.pop(key_name, None)
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    result["chars"] = len(result["content"])
    result["provider_attempts"] = sum(not item["model"].startswith("local:") for item in result["attempts"])
    result["execution_path"] = ("provider" if result["provider_attempts"] else
                                result["attempts"][0]["model"].replace(":", "_") if result["attempts"]
                                else "preflight")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--case", choices=CASES, default="principle")
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--deadline-seconds", type=float, default=45.0)
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--disable-thinking", action="store_true")
    args = parser.parse_args()
    if args.max_tokens < 1 or args.deadline_seconds <= 0:
        parser.error("token and time limits must be positive")
    credential = (sys.stdin.readline().strip() if args.key_stdin else
                  os.environ.get("INTERVIEW_COPILOT_LIVE_API_KEY", "").strip())
    if not credential:
        parser.error("provide a process environment key or --key-stdin")
    result = asyncio.run(diagnose(args, credential)) if args.diagnostic else probe(args, credential)
    print(json.dumps(result, ensure_ascii=True).replace(credential, "[REDACTED]"))


if __name__ == "__main__":
    main()
