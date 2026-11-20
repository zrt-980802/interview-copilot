"""Parse answer sections and check source locators without claiming semantic proof."""

from __future__ import annotations

from copy import deepcopy
import re

from markdown_it import MarkdownIt


_LABELS = {"开口": "conclusion", "要点": "point", "材料": "material", "待确认": "unknown",
           "方案": "hypothesis", "验证": "plan", "方向": "direction"}
_LINE = re.compile(r"^【([^】]+)】(.*)$")
_CITATION = re.compile(r"〔([^〕]*)〕")
_LOCATOR = re.compile(r"(.+?)·L(\d+)(?:-L?(\d+))?")
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable("image")


def _link_open(tokens, index, options, env):
    tokens[index].attrSet("target", "_blank")
    tokens[index].attrSet("rel", "noopener noreferrer")
    return _MARKDOWN.renderer.renderToken(tokens, index, options, env)


_MARKDOWN.renderer.rules["link_open"] = _link_open


ANSWER_TIMING_FIELDS = frozenset({
    "answer_queue_ms", "retrieval_ms", "enrichment_ms", "context_pack_ms",
    "generation_first_content_ms", "generation_total_ms",
    "task_to_first_content_ms", "task_to_complete_ms",
})


def context_sources(reference: dict) -> list[dict]:
    sources = []
    for field, kind in (("resume", "candidate_material"), ("candidate_speech", "candidate_speech"),
                        ("papers", "paper"), ("jd", "job_description"), ("enrichment", "external"),
                        ("candidate_profile","review_constraints")):
        text = reference.get(field, "")
        if text:
            sources.append({"id": "context:" + field, "source": "context:" + field,
                            "kind": kind, "heading": "", "excerpt": text,
                            "partial": field in reference.get("context_coverage", {}).get("partial_fields", [])})
    dialogue = reference.get("dialogue_context", {})
    conversation = []
    if dialogue.get("topic"):
        conversation.append("提问主题线索：" + dialogue["topic"])
    if dialogue.get("referent_question"):
        conversation.append("前文问题：" + dialogue["referent_question"])
    conversation.extend("面试官口头说明：" + text for text in dialogue.get("interviewer_background", []))
    for item in dialogue.get("candidate_turns", []):
        if item.get("question"):
            conversation.append("对应问题：" + item["question"])
        conversation.append("本人语音自述（未经核实）：" + item["text"])
    if conversation:
        sources.append({"id": "context:dialogue", "source": "context:dialogue", "kind": "reference",
                        "heading": "对话线索（非已确认经历）",
                        "excerpt": "\n".join(conversation)})
    sources.extend(deepcopy(reference.get("evidence", [])))
    for fact in reference.get("candidate_facts", []):
        if fact.get("quote") and fact.get("source"):
            sources.append({"id": "fact:" + fact["id"], "source": fact["source"],
                            "kind": "candidate_material" if fact["state"] in {"confirmed", "source_supported"} else "review_constraints",
                            "heading": fact.get("subject", ""), "excerpt": fact["quote"], "lines": fact["lines"]})
    return sources


def _citation(raw: str, sources: list[dict]) -> dict:
    text = raw[1:-1]
    locator = _LOCATOR.fullmatch(text)
    source = locator[1] if locator else text
    matches = [index for index, item in enumerate(sources) if item["source"] == source]
    result = {"raw": raw, "source": source, "source_indices": matches, "valid": False}
    if not matches:
        return {**result, "error": "citation_unknown" if locator else "citation_format_invalid"}
    if locator:
        first, last = int(locator[2]), int(locator[3] or locator[2])
        result["lines"] = [first, last]
        cursor = first
        for low, high in sorted(sources[index]["lines"] for index in matches if "lines" in sources[index]):
            if low > cursor:
                break
            if high >= cursor:
                cursor = high + 1
        if first < 1 or first > last or cursor <= last:
            return {**result, "error": "citation_out_of_range"}
    elif any("lines" in sources[index] for index in matches):
        return {**result, "error": "citation_lines_required"}
    return {**result, "valid": True}


def parse_answer(raw: str, level: str, sources=(), *, complete: bool = True) -> dict:
    """Unknown lines remain visible; material quotes require a literal source match."""
    sources = deepcopy(list(sources))
    blocks, directions, findings = [], [], []
    if level == "detail":
        if raw.strip():
            blocks.append({"kind": "explanation", "title": "", "text": raw})
    else:
        previous, fence = None, ""
        for line in raw.splitlines():
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            match = _LINE.fullmatch(line.strip()) if not fence else None
            if match and match[1] in _LABELS:
                kind, body = _LABELS[match[1]], match[2].strip()
                title, separator, text = body.partition(":::")
                block = {"kind": kind, "title": title.strip() if separator else "",
                         "text": text.strip() if separator else body}
                if kind == "direction":
                    block = {"name": title.strip(), "description": text.strip() if separator else ""}
                    directions.append(block)
                else:
                    blocks.append(block)
                previous = block
            elif previous is not None:
                key = "description" if "name" in previous else "text"
                previous[key] += "\n" + line
            elif line.strip():
                previous = {"kind": "text", "title": "", "text": line}
                blocks.append(previous)
            if marker:
                if not fence:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = ""
        if raw.strip() and not blocks:
            blocks.append({"kind": "text", "title": "", "text": raw})
        if complete and raw.strip() and not any(block["kind"] == "conclusion" for block in blocks):
            findings.append({"code": "conclusion_missing"})

    for index, block in enumerate(blocks):
        text = block["text"]
        tokens = _MARKDOWN.parse(text)
        prose = "\n".join(child.content for token in tokens if token.type == "inline"
                          for child in token.children or [] if child.type == "text")
        citations = [_citation(match[0], sources) for match in _CITATION.finditer(prose)]
        block["citations"] = citations
        if complete:
            findings.extend({"code": citation["error"], "block": index} for citation in citations if not citation["valid"])
            if prose.count("〔") != prose.count("〕"):
                findings.append({"code": "citation_incomplete", "block": index})
        if block["kind"] == "material":
            matching = [i for i, item in enumerate(sources) if item["source"] == block["title"]]
            normalized = " ".join(text.split())
            quoted = [i for i in matching if normalized and normalized in " ".join(sources[i].get("excerpt", "").split())]
            block["support"] = {"status": "quote_matched" if quoted else "unverified",
                                "source_indices": quoted or matching}
            if complete and not quoted:
                findings.append({"code": "material_quote_not_found", "block": index})
        block["html"] = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {})

    return {"version": 1, "level": level, "blocks": blocks, "directions": directions,
            "sources": sources, "findings": findings, "complete": complete}
