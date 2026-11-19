"""Bound complete model messages while keeping questions and source types intact."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re

from interview_copilot.services.corpus import clip_evidence, terms
from interview_copilot.services.answer_content import context_sources
from interview_copilot.services.dialogue import answer_style, response_plan, relevant_evidence


class ContextBudgetExceeded(ValueError):
    pass


def answer_scope(question, evidence, profile="", dialogue=None):
    dialogue = dialogue or {}
    style = answer_style(question)
    query = set(terms(question))
    project = any(item.get("kind") == "candidate_material" and
                  item.get("fact_check",{}).get("field") not in {"publication_status","period"} and
                  len(query.intersection(terms(item.get("fact_check",{}).get("subject",item.get("heading",""))))) >= 2
                  for item in evidence)
    personal = bool(re.search(r"本人|简历|经历|自我介绍|毕业|在读|你(?:以前|曾经|是否|有没有|已经|独立|在|负责|参与|做过)|"
                              r"[你我]的(?:项目|角色|工作|背景|职责)|你.*(?:做过|负责过|上线过|参与过)|"
                              r"\b(?:my|your)\s+(?:project|experience|work|role|background|resume)\b|\b(?:have|did) you\b",question,re.I))
    personal = personal or style in {"experience", "introduction"}
    personal = personal or (style == "hr" and bool(re.search(r"薪资|薪酬|待遇|离职|婚姻|未婚|已婚|住在哪|住哪里|通勤", question)))
    design = bool(re.search(r"假设|假如|如果(?:要|让|由)|设计(?:一个|一套)|如何设计|怎么设计|\bhow would\b|\bsuppose\b|\bdesign (?:a|an)\b",question,re.I))
    mode = "design" if design else "experience" if personal or project else "technical"
    scope = {"mode":mode,"model_knowledge_allowed":True,
             "answer_style": style,
             "response_plan": {**response_plan(question), "allow_direction": False},
             "reference_policy":"optional_references" if evidence else "model_knowledge",
             "knowledge_guidance":"通用知识可以直接作答；资料只提供相关补充，不是知识白名单。忽略关联低的片段，不因资料没有覆盖而拒答，不强行联系本人项目或虚构引用。"}
    if mode == "experience":
        scope["boundary"] = "只将明确支持的部分作为个人经历；其余具体实现以通用方案或待确认表述。未知不能用‘没有/没做过’直接否认。"
    if mode == "experience" and project and re.search(r"如何|怎么|怎样|实现|处理|机制",question):
        scope["mode"] = "project_method"
        scope["boundary"] = "先说原文支持的做法范围，再用‘一种可行实现’解释机制。原文未明确的主键、阈值、校验和冲突裁决规则不能写成本人已实施。"
    if mode == "design":
        scope["boundary"] = "按题目约束自主提出方案、推导和取舍；缺少约束时明确合理假设，不要求资料中已有实现，不写成个人已完成的项目。"
    if mode == "technical" and dialogue.get("project_followup") and re.search(r"如何|怎么|怎样|实现|处理|机制", question):
        scope["mode"] = "project_method"
        scope["boundary"] = "这是对前述项目方法的追问；只将有证据的实现作为本人做法，未记载的机制以可行方案解释。"
    if mode == "experience" and "graduation_date" in profile and "材料冲突" in profile:
        scope["temporal_boundary"] = "毕业时间有冲突，当前毕业/在读状态未确认。自我介绍不能断言当前在读或已毕业，也不要推荐未经确认的在读话术；可陈述材料支持的研究方向与论文情况。"
    return scope


def scope_reference(question, reference):
    reference = deepcopy(reference)
    reference["evidence"] = relevant_evidence(question, reference.get("evidence", []), reference.get("dialogue_context"))
    scope = answer_scope(question,reference.get("evidence",[]),reference.get("candidate_profile",""),reference.get("dialogue_context"))
    reference["answer_scope"] = scope
    if scope["mode"] in {"technical","design"}:
        reference["resume"] = ""
        if not reference.get("evidence"):
            reference["papers"] = ""
    return reference


def clarification_for(history: list[dict], candidate_speech: str = "") -> str | None:
    """Handle only standalone deictic questions with no conversational referent."""
    if not history or history[-1].get("role") != "user" or candidate_speech.strip():
        return None
    if any(message.get("content", "").strip() for message in history[:-1]):
        return None
    question = re.sub(r"\s+", "", history[-1].get("content", "")).rstrip("?？。!！")
    if re.fullmatch(
        r"(?:那|那么)?(?:为什么(?:要)?(?:这样|这么)(?:做|设计|实现|处理)"
        r"|(?:这个|这套|该)(?:方案|设计|做法)(?:有什么问题|有啥问题|有什么缺点|为什么可行)"
        r"|(?:它|这个|这)(?:的)?(?:边界|风险|问题)(?:呢|是什么|有哪些)?)", question
    ):
        return "【开口】你指的是哪种具体做法或方案？"
    return None


def message_chars(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False))


def _excerpt(text: str, cap: int, query: str) -> str:
    if len(text) <= cap:
        return text
    if cap < 40:
        return ""
    units = [part for line in text.splitlines() if line.strip()
             for part in (line[offset:offset + 600] for offset in range(0, len(line), 600))]
    query_terms = set(terms(query))
    ranked = sorted(range(len(units)), key=lambda index: (
        index != 0, -len(query_terms.intersection(terms(units[index]))) / math.sqrt(max(1, len(units[index])))))
    # Retain the introduction, then choose relevant units in original source order.
    selected, used = {}, 0
    for index in ranked:
        if used >= cap - 8:
            break
        piece = units[index][:cap - used - 8]
        if not piece:
            continue
        selected[index] = piece
        used += len(piece) + 8
    return "\n[...]\n".join(selected[index] for index in sorted(selected))[:cap]


def _history_within(history, cap):
    result = list(history)
    while result and len(json.dumps(result, ensure_ascii=False)) > cap:
        result.pop(0)
        while result and result[0]["role"] == "assistant":
            result.pop(0)
    return result


def _evidence_within(items, cap):
    result = []
    for item in items:
        if len(json.dumps(result + [item], ensure_ascii=False)) <= cap:
            result.append(item)
            continue
        overhead = len(json.dumps(result + [clip_evidence(item, 0)], ensure_ascii=False))
        if cap - overhead < 80:
            break
        clipped = clip_evidence(item, cap - overhead)
        while clipped["excerpt"] and len(json.dumps(result + [clipped], ensure_ascii=False)) > cap:
            clipped = clip_evidence(item, len(clipped["excerpt"]) - 1)
        if clipped["excerpt"]:
            result.append(clipped)
        break
    return result


@dataclass(frozen=True)
class ContextPack:
    messages: list[dict]
    original_chars: int
    sent_chars: int
    max_chars: int
    excerpted: tuple[str, ...]
    evidence: list[dict]
    sources: list[dict]


def compose_context(system: str, reference: dict, history: list[dict], *,
                    request: dict | None = None, max_chars: int = 16000) -> ContextPack:
    reference, request = deepcopy(reference), deepcopy(request)
    history = [dict(message) for message in history if message.get("role") in {"user", "assistant"}]
    if request is None:
        if not history or history[-1]["role"] != "user":
            raise ContextBudgetExceeded("缺少当前问题，未发送模型请求")
        current, recent = history[-1], history[:-1]
        query = current["content"]
    else:
        current, recent = None, request.pop("recent_ai_conversation", [])
        recent = [message for message in recent if message.get("role") in {"user", "assistant"}]
        query = request["question"] + " " + request.get("direction", "")
    def render(values, excerpted=()):
        data = {**reference, **{key: values[key] for key in reference if key in values}}
        if excerpted:
            data["context_coverage"] = {"partial_fields": list(excerpted),
                                        "note": "资料有节选或省略；缺失不能视为否定事实。"}
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]
        if request is None:
            messages += values["history"] + [current]
        else:
            body = {**request, "ai_quick_suggestion": values["ai_quick_suggestion"],
                    "recent_ai_conversation": values["history"]}
            messages.append({"role": "user", "content": json.dumps(body, ensure_ascii=False)})
        return messages

    original = {key: reference.get(key, "") for key in
                ("resume", "papers", "jd", "candidate_speech", "enrichment", "candidate_profile")}
    original.update(evidence=reference.get("evidence", []), history=recent,
                    ai_quick_suggestion=request.get("ai_quick_suggestion", "") if request else "")
    messages = render(original)
    original_chars = message_chars(messages)
    if original_chars <= max_chars:
        return ContextPack(messages, original_chars, original_chars, max_chars, (), original["evidence"],
                           context_sources(json.loads(messages[1]["content"])))

    weights = {"resume": 0.20, "candidate_speech": 0.10, "evidence": 0.22, "history": 0.14,"candidate_profile":0.16,
               "jd": 0.06, "papers": 0.06, "enrichment": 0.04, "ai_quick_suggestion": 0.02}
    empty = {key: ([] if isinstance(value, list) else "") for key, value in original.items()}
    mandatory_chars = message_chars(render(empty, tuple(original)))
    available = max_chars - mandatory_chars
    if available < 0:
        raise ContextBudgetExceeded("当前问题或方向超过上下文预算，请缩短后重试；未发送模型请求")
    sizes = {key: len(json.dumps(value, ensure_ascii=False)) if isinstance(value, list) else len(value)
             for key, value in original.items()}
    profile_size = len(json.dumps(original["candidate_profile"],ensure_ascii=False)) - 2
    if profile_size > available:
        raise ContextBudgetExceeded("本人核对记录超过上下文预算，请增加模型上下文预算后重试；未发送模型请求")
    caps = {key: min(sizes[key], int((available - profile_size) * weight / (1 - weights["candidate_profile"])))
            for key,weight in weights.items() if key != "candidate_profile"}
    caps["candidate_profile"] = profile_size
    remaining = available - sum(caps.values())
    for key in weights:
        extra = max(0,min(remaining, sizes[key] - caps[key]))
        caps[key] += extra
        remaining -= extra

    while True:
        values = {key: _excerpt(value, caps[key], query) for key, value in original.items() if isinstance(value, str)}
        values["history"] = _history_within(recent, caps["history"])
        values["evidence"] = _evidence_within(original["evidence"], caps["evidence"])
        excerpted = tuple(key for key in original if values[key] != original[key])
        messages = render(values, excerpted)
        sent_chars = message_chars(messages)
        if sent_chars <= max_chars:
            return ContextPack(messages, original_chars, sent_chars, max_chars, excerpted, values["evidence"],
                               context_sources(json.loads(messages[1]["content"])))
        for key in ("enrichment", "ai_quick_suggestion", "history", "jd", "papers", "evidence", "resume", "candidate_speech"):
            if caps[key] > 0:
                caps[key] = max(0, caps[key] - (sent_chars - max_chars) - 32)
                break
        else:
            raise ContextBudgetExceeded("当前问题超过上下文预算，未发送模型请求")
