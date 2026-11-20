"""Local question intent and source-labelled conversational referents."""

import re


_TOPICS = (
    (r"肝病|肝脏|肝癌", "肝病知识图谱项目"),
    (r"MirrorRE|镜像|少样本关系抽取", "MirrorRE关系抽取研究"),
    (r"汽车.*(?:图谱|诊断)|(?:传感器|控制器).*图谱", "汽车知识图谱与故障诊断"),
    (r"金融|研报|财报|利润率", "金融数据与RAG"),
    (r"知识图谱|实体.*关系.*抽取", "知识图谱项目"),
    (r"订单|下单|扣款", "订单处理"),
    (r"缓存|Redis", "缓存与数据一致性"),
    (r"Agent|智能体", "Agent系统"),
)
_SWITCH = re.compile(r"下一个问题|换个话题|换一道|接下来|再问(?:你)?一个|\bnext question\b", re.I)
_DEICTIC = re.compile(r"这(?:个|块|部分|些|种|里|套)|那(?:个|块|部分)|你们|它|刚(?:才|刚)|上述|前面|上面|团队|工作量|多少人|几个人|\b(?:it|that|this|your team)\b", re.I)
_EXPERIENCE = re.compile(
    r"自我介绍|简历|个人经历|你们.*(?:人|团队|分工|规模|工作量)|(?:团队|项目).*(?:几人|多少人|几个人|工作量)|"
    r"工作量|你.*(?:做过|做的|主导|带领|负责|参与|学过|用过|使用过|接手|遇到|从零|从0)|"
    r"你(?:的|这个|这块).*(?:角色|职责|成果|项目)|\b(?:have you|did you|your role|your experience)\b", re.I)
_HR = re.compile(r"薪资|薪酬|待遇|离职|加班|婚姻|未婚|已婚|对象.*上海|住在哪|住哪里|通勤|城市发展|哪个城市|求职|拿到.*offer|几所学校|两个硕士", re.I)
_DESIGN = re.compile(r"假设|假如|如果(?:要|让|由)|如何设计|怎么设计|设计(?:一个|一套)|场景题|\bhow would\b|\bsuppose\b", re.I)


def answer_style(question):
    if re.search(r"(?:有什么|哪些|什么).*(?:想问|想要了解|想了解)|有什么问题.*问我", question):
        return "counter_question"
    if _DESIGN.search(question):
        return "design"
    if _HR.search(question):
        return "hr"
    if re.search(r"自我介绍|介绍.*自己", question):
        return "introduction"
    if _EXPERIENCE.search(question):
        return "experience"
    if re.search(r"怎么评估|如何评估|质量|准确率|召回率|schema|抽取效果", question, re.I):
        return "evaluation"
    if re.search(r"利润率|怎么计算|如何计算|怎么找数据", question):
        return "calculation"
    return "technical"


def response_plan(question):
    """Limit the answer to the facets actually requested, not every retrieved fact."""
    if re.search(r"多少.*人|几个人|几名|团队.*人数", question) and not re.search(r"怎么设计|如何设计|假设", question):
        return {"focus": "团队人数和必要分工；不介绍无关项目规模和质量指标", "max_points": 0, "body_chars": 100}
    if re.search(r"工作量|周期|多久", question):
        return {"focus": "用有依据的周期、数据规模或职责解释工作量；不重复上一题的抽取方法", "max_points": 1, "body_chars": 180}
    if re.search(r"(?:学过|用过|使用过).*(?:吗|么|没)|是否.*(?:学过|用过|使用过)", question):
        return {"focus": "是否有使用经历、掌握范围；未知不猜测", "max_points": 0, "body_chars": 100}
    if answer_style(question) == "counter_question":
        return {"focus": "1至2个可直接问面试官的问题；不用解释提问的好处", "max_points": 0, "body_chars": 140}
    if "利润率" in question:
        return {"focus": "指标与期间口径、取数和可复算计算、原因证据与回答；不展开完整系统教程", "max_points": 2, "body_chars": 280}
    return {"focus": "只回答最后的当前问题；前文用于指代，不补答已经换掉的旧题", "max_points": 2, "body_chars": 220}


def question_context(question, previous=None):
    """Resolve only conversational hints; a topic is not a verified personal fact."""
    previous = previous or {}
    topic = next((name for pattern, name in _TOPICS if re.search(pattern, question, re.I)), "")
    # Preserve a specific project while a follow-up uses a broader term such as 图谱.
    if topic == "知识图谱项目" and "图谱" in previous.get("topic", "") and not _SWITCH.search(question):
        topic = previous["topic"]
    inherited = not topic and bool(_DEICTIC.search(question)) and not _SWITCH.search(question)
    if inherited:
        topic = previous.get("topic", "")
    style = answer_style(question)
    project_followup = bool(topic and re.search(r"项目|图谱|研究", topic) and
                            re.search(r"这(?:块|部分|里|个)|刚(?:才|刚)|你们|它|工作量|团队", question))
    referent = previous.get("topic_question", "") if inherited else ""
    if inherited and not referent:
        referent = previous.get("question", "")
    anchor = topic or referent
    return {
        "question": question,
        "topic": topic,
        "topic_question": (referent or question)[:500],
        "referent_question": referent[:500],
        "answer_style": style,
        "project_followup": project_followup,
        "retrieval_query": (anchor + " " + question).strip() if inherited and anchor else question,
        "note": "主题与指代是从面试官问题归纳的线索，不证明本人做过；实际发言和AI建议分开使用。",
    }


def evidence_queries(question, dialogue=None):
    """Use a focused facet for terse project follow-ups without losing the original."""
    dialogue = dialogue or {}
    query = dialogue.get("retrieval_query") or question
    facets = []
    if re.search(r"工作量|多久|周期|规模", question):
        facets.append("工作量 周期 数据规模 职责")
    if re.search(r"多少.*人|几个人|团队|分工|协作", question):
        facets.append("团队 人数 分工 本人角色 职责")
    if re.search(r"评估|质量|schema|准确率|召回率", question, re.I):
        facets.append("标注 schema 质量 验证 错误 指标")
    focused = " ".join(part for part in (dialogue.get("topic", ""), " ".join(facets)) if part)
    return list(dict.fromkeys([query] + ([focused] if facets and dialogue.get("topic") else [])))


def relevant_evidence(question, evidence, dialogue=None):
    """Do not cite another domain's personal project as proof of a design answer."""
    target = (dialogue or {}).get("topic") or question_context(question)["topic"]
    domains = {"肝病知识图谱项目", "汽车知识图谱与故障诊断", "金融数据与RAG"}
    if target not in domains:
        return evidence
    result = []
    for item in evidence:
        text = item.get("heading", "") + " " + item.get("excerpt", "")
        source_topic = question_context(text)["topic"]
        if item.get("kind") == "candidate_material" and source_topic in domains and source_topic != target:
            explicit = any(re.search(pattern, question, re.I) for pattern, name in _TOPICS if name == source_topic)
            if not explicit:
                continue
        result.append(item)
    return result
