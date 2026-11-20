"""Markdown export preserving original answers and the sources used at generation."""

import re


def fenced(text):
    text = text or ""
    fence = "`" * max(3, max((len(match[0]) + 1 for match in re.finditer(r"`+", text)), default=3))
    return f"{fence}text\n{text}\n{fence}"


def export_session(session):
    lines = ["# 面试复盘", "", "> " + (session["title"] or str(session["id"])).replace("\n", " "),
             f"> 开始 {session['started_at']} | 结束 {session['end_at'] or '进行中'}", ""]
    for index, turn in enumerate(session["turns"], 1):
        lines.extend([f"## 问题 {index} · {turn['ts']}", "", fenced(turn["question"]), ""])
        if turn.get("self_context"):
            lines.extend(["### 当时的发言记录", "", fenced(turn["self_context"]), ""])
        answers = [("回答建议", turn.get("quick_card"), turn.get("quick_document"), turn.get("quick_meta"))]
        answers.extend(("深入 · " + item["direction"], item["content"], item.get("document"), item.get("meta"))
                       for item in turn.get("details", []))
        for title, content, document, meta in answers:
            if not content:
                continue
            lines.extend(["### " + title.replace("\n", " "), "", fenced(content), ""])
            if meta and meta.get("model"):
                lines.extend(["> 模型 " + meta["model"].replace("\n", " ") + f" | 尝试 {meta.get('attempt', 0)}", ""])
            for source_index, source in enumerate((document or {}).get("sources", []), 1):
                locator = " · L" + "-L".join(map(str, source["lines"])) if source.get("lines") else ""
                lines.extend([f"#### 来源 {source_index}", "", fenced(source["source"] + locator), "",
                              fenced(source.get("excerpt", "")), ""])
        for version, revision in enumerate(turn.get("revisions", []), 1):
            lines.extend([f"### 补充前的版本 {version}", "", fenced(revision["question"]), ""])
            if revision["answer"]:
                label = "当时的回答" if revision["answer_status"] == "complete" else "当时已生成的片段"
                lines.extend([f"> {label}", "", fenced(revision["answer"]), ""])
        lines.extend(["---", ""])
    return "\n".join(lines)
