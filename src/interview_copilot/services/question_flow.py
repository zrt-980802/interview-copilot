"""Conservative text cues for speech fragments; silence alone does not define a turn."""

import re

from interview_copilot.services.corpus import terms


def is_repetitive_text(text):
    compact = re.sub(r"[\W_]+","",text).casefold()
    return bool(re.fullmatch(r"(.{1,8})\1{3,}",compact))


def is_non_question(text):
    compact = re.sub(r"[\W_]+","",text).casefold()
    if not compact:
        return True
    if re.fullmatch(r"(?:嗯|啊|哦|好|好的|喂|谢谢|谢谢大家|感谢观看|感谢收看|thankyou|thanks|拜拜|bye|哈|那个|然后|咳嗽|清嗓|cough)+",compact):
        return True
    if text.startswith("术语") and "\ufffd" in text and not has_question_cue(text):
        return True
    return is_repetitive_text(text)


def has_question_cue(text):
    return bool(re.search(r"[?？]|怎么|如何|为什么|什么|哪些|多少|几个|是否|有没有|能否|可以.*吗|讲(?:一讲|讲)|介绍|解释|说一下|谈谈|怎么看|想问|想要了解|想了解|\b(?:how|why|what|can you)\b", text, re.I))


def is_background(text):
    """Only clear statements are suppressed; scenario constraints remain available."""
    if re.search(r"[?？]|怎么|如何|为什么|什么|哪些|多少|几个人|是否|有没有|能否|吗|"
                 r"请|设计|给出|写出|分析|介绍(?:一下)?[你您]|讲(?:一讲|讲)|说一下", text):
        return False
    return bool(re.search(r"(?:我们|这边|后续|总体|整体).*(?:工作时间|上班时间|上半时间|培训|面试流程|签订|主要.*文本|都是文本|主要.*图表)|"
                          r"(?:后续|最后|下周).*(?:面试|终面|中面)|具体.*(?:微信|通知).*时间|总体流程|总体流场", text))


def is_correction(previous, fragment):
    compact = fragment.strip(" 。,.，!?？")
    explicit = re.match(r"^(?:不[，, ]*是|不是|我(?:是)?说|准确(?:地)?说|更正|纠正|应该是)[：:,， ]*(.+)$", compact)
    term = explicit[1].strip() if explicit else compact
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9.+#_-]{1,24}", term):
        return False
    if re.search(r"下一个|换个|接下来|再问", fragment):
        return False
    return bool(explicit or re.search(r"这个|那个|所谓|(?:IG|REG|RAG|LIM|LLM)|了不了解|时序", previous, re.I))


def looks_unfinished(text):
    text = text.rstrip("。.,，?？!！ ")
    return bool(re.search(r"(?:如果|假设|比如|例如|以及|并且|然后|但是|或者说|包括|分别是|一个|一种|关于|当|这些|那么|那|下面可能)$",text)
                or (re.match(r"^(?:如果|假设|假如)",text) and not re.search(r"怎么|如何|为什么|什么|哪些|是否|写出|设计|讲讲|解释",text)))


def is_continuation(previous, fragment):
    if re.search(r"下一个问题|换个话题|换一道|再问(?:你)?一个|接下来|(?:然后|另外|再)(?:讲讲|介绍|聊聊|谈谈)|\bnext question\b",fragment,re.I):
        return False
    if looks_unfinished(previous):
        return True
    if is_correction(previous, fragment):
        return True
    if re.match(r"^(?:这些|那些).*(?:schema|定义|约束)", fragment, re.I) and re.search(r"抽取|评估|这些", previous):
        return True
    if re.match(r"^(?:需要做|还是说|这种的|你可以想|可以想|我想问一下)", fragment) and not has_question_cue(previous):
        return True
    if re.match(r"^(?:补充|也就是|我是说|准确地说|比如|例如|尤其|以及|并且|每一(?:轮|步)|结果以|结果怎么|还有一点)",fragment):
        return True
    shared = set(terms(previous)).intersection(terms(fragment))
    return len(shared) >= 2 and bool(re.match(r"^(?:那|然后|还有|具体|这里|这个|它|分别|同时|结果)",fragment))
