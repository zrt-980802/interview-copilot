# -*- coding: utf-8 -*-
"""动态热词词表：从简历/JD/论文标题提取专名，显著提升 whisper 专名识别率。

原理（实测 bench）：同段音频无热词把 MirrorRE 听成 MirrorA，带热词立即正确。
固定写死术语 vs 按候选人材料动态生成——后者覆盖面试真正会被问到的词。
"""
import re


def build_hotwords(resume_text: str = "", jd_text: str = "",
                   paper_titles: list | None = None,
                   base: str = "") -> str:
    """组合热词串。优先级：专名(英文/大小写混排) > 中文技术词。"""
    parts = []
    if base:
        parts.append(base)
    seen = set()

    def add_word(w):
        w = w.strip().strip("：:,.()[]（）《》\"'’")
        if not w or len(w) < 2:
            return
        if w in seen:
            return
        seen.add(w)
        parts.append(w)

    # 论文标题：只提取"专名"而非整句英文——中文语音转写需要的是 MirrorRE/
    # GraphRAG 这类锚词，长句会占爆 whisper initial_prompt 预算(224 token)
    if paper_titles:
        for t in paper_titles:
            if not t:
                continue
            # 1) 标题首词（MirrorRE / Alignment / GuideRE）
            m = re.match(r"([A-Za-z][A-Za-z0-9_-]*)", t.strip())
            if m:
                add_word(m.group(1))
            # 2) 驼峰/混合大小写专名（GraphRAG、LLaMA、CoT…）
            for m in re.finditer(r"[A-Za-z]+[0-9]?[A-Z][a-z0-9]+", t):
                tok = m.group(0)
                if re.search(r"[a-z]", tok):     # 含小写才算驼峰，排除纯大写
                    add_word(tok)
    for text in (resume_text, jd_text):
        if not text:
            continue
        # 技术栈常见写法：Java/Go/Python/MySQL 等（含大写英文）
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#._-]{1,20}", text):
            tok = m.group(0)
            if re.search(r"[A-Z]", tok) or tok.lower() in (
                    "python", "mysql", "redis", "kafka", "docker", "spring",
                    "transformers", "pytorch", "tensorflow", "neo4j"):
                add_word(tok)
    # 中文高频技术词
    zh_words = ("多模态 关系抽取 知识图谱 检索增强 少样本 索引 事务 高并发 分布式 "
                "微服务 缓存 消息队列 大模型 智能体 语义对齐 时序图谱 GraphRAG RAG "
                "Prompt工程 模型微调")
    for w in zh_words.split():
        add_word(w)

    if not parts:
        return ""
    return "后端面试可能出现的术语：" + " ".join(parts[:60])
