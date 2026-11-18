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
    return is_repetitive_text(text)


def looks_unfinished(text):
    text = text.rstrip("。.,，?？!！ ")
    return bool(re.search(r"(?:如果|假设|比如|例如|以及|并且|然后|但是|或者说|包括|分别是|一个|一种|关于|当)$",text)
                or (re.match(r"^(?:如果|假设|假如)",text) and not re.search(r"怎么|如何|为什么|什么|哪些|是否|写出|设计|讲讲|解释",text)))


def is_continuation(previous, fragment):
    if re.search(r"下一个问题|换个话题|换一道|再问(?:你)?一个|接下来|(?:然后|另外|再)(?:讲讲|介绍|聊聊|谈谈)|\bnext question\b",fragment,re.I):
        return False
    if looks_unfinished(previous):
        return True
    if re.match(r"^(?:补充|也就是|我是说|准确地说|比如|例如|尤其|以及|并且|每一(?:轮|步)|结果以|结果怎么|还有一点)",fragment):
        return True
    shared = set(terms(previous)).intersection(terms(fragment))
    return len(shared) >= 2 and bool(re.match(r"^(?:那|然后|还有|具体|这里|这个|它|分别|同时|结果)",fragment))
