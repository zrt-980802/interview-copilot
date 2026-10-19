# -*- coding: utf-8 -*-
"""GLM 主通道 + DeepSeek 兜底的流式补全封装。密钥只从环境变量读取，不落盘、不打日志。

两级作答：
- 快速层 stream_quick：开口样板句 + 要点 + 可选展开方向（面试官问完5秒内可照说）
- 深度层 stream_detail：按用户选择的方向展开细节/数字/追问预备
"""
import json
import os

import httpx


def _load_env_files():
    """把 Hermes 的 .env 里与本项目相关的 KEY 导入进程环境（不覆盖已有值）。"""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.join(os.path.expanduser("~"), ".env"),
    ]
    for p in candidates:
        try:
            if not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):]
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and v:
                        os.environ.setdefault(k, v)
        except Exception:
            pass


_load_env_files()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
    LLM_CFG = json.load(f)["llm"]

QUICK_PROMPT = """你是技术面试实时辅助器的「快速反应层」，同时是一名严格的资深技术面试官陪练。面试官刚问完问题，候选人必须立刻开口。你给的不是"学习建议"，而是**能直接照说的专业答案骨架**——密度优先：一句话里要有专业信号（机制名/设计取舍/数字/边界），宁可一句有干货，不要三句空转。

严格按以下行格式输出（禁止 Markdown 加粗，禁止任何多余解释或前后空行）：
【开口】<一句话结论或定位，15~30字，直接照说；禁止"好的/这个问题问得好/谢谢/我觉得/首先"等寒暄与口头禅；确实不熟时用"我先说我的理解——"的诚实锚句式开场>
【要点】<提醒短语≤12字> ::: <一句20~40字的专业逻辑：像给面试官讲清道理，必须含至少一个专业信号（机制/取舍/复杂度/数据/边界）> ｜ 例：<1个能说出口的具体例子：优先简历/论文真实项目与数字；没有则用业界公认案例并标"(例：...)"。绝不编造论文/简历里不存在的数字>
【要点】…（最多4条，按题型取舍，宁缺毋滥）
【方向】<方向名2~8字> ::: <一句话说明这个方向会展开什么>（≤2条，仅确实值得展开时给）

先判断题型，再按对应骨架组织要点（骨架是暗线，不要写成流程介绍）：
- 技术原理/为什么类：本质一句话 → 关键机制 → 为什么这么设计(权衡) → 边界/适用场景
- 项目/论文类：我的角色 → 核心做法 → 如何证明有效(指标/消融/对比) → 卡过的最难的点怎么解
- 行为/经历类：情境 → 我做的可验证动作 → 量化结果 → 复盘学到什么
- 开放设计/方案类：先给判断框架(目标→约束→取舍)，再落到该岗位会用的技术栈具体方案

规则：
1. 每条【要点】必须含 ::: 说明 与 ｜ 例： 两段，缺一不可。
2. 涉及研究/论文：落候选人的真实工作（MirrorRE/GuideRE/AoG等别名）与真实数字。
3. 提供了 JD：回答像为岗位量身定做——贴合职责关键词（Agent/RAG/微调/Prompt/架构）；自我介绍/动机题必须结合 JD。
4. 发散/看法题：给有立场的观点 + 依据，别骑墙。
5. 全部输出不超过7行。"""

DETAIL_PROMPT = """你是技术面试实时辅助器的「深度展开层」，资深技术面试官陪练。候选人已按快速卡说完骨架，现在按其选择的方向给出**可继续照说的弹药**。你已知道他刚说过什么——只补增量、加深、防追问，绝不重复他已讲过的内容。

输出紧凑 Markdown 卡片（总量≤260字），直接是接着说的话术：
**深挖**｜<该方向的3~4条细节，①②③逐条为"可直接照说的一句话"，每条至少含一个专业信号：机制名/直觉解释/复杂度/数据/与业界方案的取舍对比。面试官要听的是你**为什么这么选、代价是什么、边界在哪**>
**⚠️ 追问预备**｜<最可能被追的1~2个问题（为什么这样设计/换场景怎么办/复杂度与扩展性/和XX相比）+ 每个配一句可照说的应对>

若指定方向为"完整版"，改用以下结构：
**背景 S**｜<一句话场景，引用简历真实项目>
**做法 A**｜① … ② … ③ …（每条附机制名/取舍/数字，体现设计权衡而非流水账）
**结果 R**｜<量化指标；给一两个对标或业界参照数字增强说服力>
**⚠️ 可能追问**｜<2个追问+应对>

引用规则：研究内容只引候选人真实论文（别名如 MirrorRE/GuideRE/AoG）与简历真实内容；论文/简历里没有的数字绝不编造，需要外部量级时用业界公认口径并标"(约X)"；提供了补充资料（知识库/网页摘要）时把其中的事实、数字转化为自己的例子使用。
禁止开场白、寒暄、"首先/总的来说"等套话，直接输出卡片。"""


class LLMClient:
    def __init__(self):
        self.client = httpx.Client(timeout=60.0)

    # ---------- 通道解析 ----------
    def _resolve_primary(self, prefer_model: str = ""):
        key = None
        for env in LLM_CFG.get("api_key_env", []):
            v = os.environ.get(env, "").strip()
            if v:
                key = v
                break
        if not key:
            return None
        base = LLM_CFG["base_url"].rstrip("/")
        # DeepSeek 官方端点不支持 thinking 参数（实测会静默忽略/行为异常）
        extra = {}
        if LLM_CFG.get("disable_thinking", True) and "bigmodel" in base:
            extra = {"thinking": {"type": "disabled"}}
        # 快速卡用无推理模型(quick_model)，深挖用质量模型(detail_model)
        model = prefer_model or LLM_CFG.get("model") or LLM_CFG["detail_model"]
        return {
            "base_url": base,
            "model": model,
            "key": key,
            "extra": extra,
        }

    def _resolve_fallback(self):
        envs = LLM_CFG.get("fallback_api_key_env", [])
        if not envs:
            return None
        key = None
        for env in envs:
            v = os.environ.get(env, "").strip()
            if v:
                key = v
                break
        if not key:
            return None
        return {
            "base_url": LLM_CFG.get("fallback_base_url", "").rstrip("/"),
            "model": LLM_CFG.get("fallback_model", ""),
            "key": key,
        }

    def _candidates(self, prefer_model: str = ""):
        cands = [c for c in (self._resolve_primary(prefer_model),
                             self._resolve_fallback()) if c]
        return cands

    # ---------- 流式执行 ----------
    def _stream(self, candidates, messages, on_delta, on_done, on_error):
        last_err = ""
        for cand in candidates:
            ok = False
            full_parts = []
            try:
                with self.client.stream(
                    "POST",
                    f"{cand['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {cand['key']}",
                             "Content-Type": "application/json"},
                    json={
                        "model": cand["model"],
                        "messages": messages,
                        "stream": True,
                        "temperature": LLM_CFG.get("temperature", 0.6),
                        "max_tokens": LLM_CFG.get("max_tokens", 800),
                        **cand.get("extra", {}),
                    },
                ) as resp:
                    if resp.status_code != 200:
                        last_err = f"HTTP {resp.status_code} ({cand['model']})"
                        resp.read()
                        continue
                    for line in resp.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            ok = True
                            break
                        try:
                            obj = json.loads(payload)
                            d_obj = obj["choices"][0].get("delta") or {}
                            # 只取 content；丢弃 reasoning_content（v4-flash 推理流不进卡片）
                            delta = d_obj.get("content") or ""
                        except Exception:
                            continue
                        if delta:
                            full_parts.append(delta)
                            on_delta(delta)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e} ({cand['model']})"
                continue
            if ok and full_parts:
                on_done("".join(full_parts))
                return
        on_error(last_err or "所有模型通道均失败")

    def _run(self, messages, on_delta, on_done, on_error, prefer_model: str = ""):
        cands = self._candidates(prefer_model)
        if not cands:
            on_error("未找到 GLM_API_KEY/ZAI_API_KEY 或 DEEPSEEK_API_KEY 环境变量")
            return
        self._stream(cands, messages, on_delta, on_done, on_error)

    # ---------- 两级作答接口 ----------
    def stream_quick(self, history: list, resume: str, on_delta, on_done, on_error,
                     self_recent: str = "", papers: str = "", jd: str = ""):
        """快速层。history=[{"role","content"}]，最后一项应为面试官的问题(user)。
        self_recent: 候选人本人最近的真实发言(麦克风转写)。
        papers: 论文摘要卡+相关节选(由服务端检索组装)。jd: 目标岗位JD。"""
        self_note = (f"\n【候选人最近的真实发言（你生成的开口句必须自然衔接他已说的内容，"
                     f"避免重复他刚讲过的要点）】\n{self_recent}" if self_recent else "")
        paper_note = (f"\n{papers}" if papers else "")
        jd_note = (f"\n【目标岗位JD（回答向此岗位对齐）】\n{jd}" if jd else "")
        messages = [{"role": "system", "content": QUICK_PROMPT},
                    {"role": "system", "content": f"【候选人简历】\n{resume}"},
                    {"role": "system", "content": self_note},
                    {"role": "system", "content": paper_note},
                    {"role": "system", "content": jd_note}] + history[-6:]
        # 快速卡：无推理模型（v4-flash 有隐藏推理流会拖慢 7s+）
        self._run(messages, on_delta, on_done, on_error,
                  prefer_model=LLM_CFG.get("quick_model", ""))

    def stream_detail(self, question: str, quick_text: str, direction: str,
                      history: list, resume: str, on_delta, on_done, on_error,
                      self_recent: str = "", papers: str = "", enrich: str = "",
                      jd: str = ""):
        """深度层：按方向展开。direction 为具体方向名或 '完整版'。"""
        tail = ""
        if history:
            prev = history[-4:]
            tail = "\n".join(f"[{'Q' if m['role'] == 'user' else 'A'}] {m['content'][:120]}"
                             for m in prev) + "\n"
        self_note = (f"\n【候选人最近的真实发言（深挖内容要接住他已说的，别重复、补增量）】\n{self_recent}"
                     if self_recent else "")
        user_msg = (f"{tail}【本次面试官问题】{question}\n"
                    f"【候选人已说完的快速版本】\n{quick_text}"
                    f"{self_note}\n"
                    f"【请按此方向深挖】{direction}")
        messages = [{"role": "system", "content": DETAIL_PROMPT},
                    {"role": "system", "content": f"【候选人简历】\n{resume}"},
                    {"role": "system", "content": papers or ""},
                    {"role": "system", "content": enrich or ""},
                    {"role": "system",
                     "content": f"【目标岗位JD（深挖方向尽量与岗位职责挂钩）】\n{jd}" if jd else ""},
                    {"role": "user", "content": user_msg}]
        # 深挖卡：质量优先模型（可容忍推理延迟）
        self._run(messages, on_delta, on_done, on_error,
                  prefer_model=LLM_CFG.get("detail_model", ""))

    def stream_answer(self, history, resume, on_delta, on_done, on_error):
        """旧接口兼容：等价 stream_quick。"""
        self.stream_quick(history, resume, on_delta, on_done, on_error)
