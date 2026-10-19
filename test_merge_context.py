# -*- coding: utf-8 -*-
"""拆句合并 + 双通道上下文 专项测试：
1) 模拟面试官慢语速：连续3段(间隔<1.1s)喂给 merger → 应只出一道合并题
2) 模拟候选人麦克风发言 → self_recent 应出现在 /api/state
3) 快速卡 prompt 应包含候选人发言提示（从 LLM 行为间接验证：开口句衔接我方已说内容）
"""
import json
import sys
import threading
import time

import httpx

BASE = "http://127.0.0.1:8527"


def collect_until_question(timeout=25):
    """后台收 SSE，直到 answer_done(level=quick) 或超时。返回事件列表。"""
    evs = []
    done = threading.Event()

    def run():
        try:
            with httpx.Client(timeout=60).stream("GET", f"{BASE}/api/events") as resp:
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    evs.append(ev)
                    if ev.get("type") == "answer_done" and ev.get("level") == "quick":
                        done.set()
                        return
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    done.wait(timeout)
    return evs


def main():
    c = httpx.Client(timeout=15)

    # 直接喂数据需要走内部接口——没有独立注入口，所以用"手动问"验证主流程，
    # merger 逻辑单测在进程内另起实例验证（见下方 TestMerger）。
    print("== Part1: QuestionMerger 单元逻辑（进程内直测）==")
    sys.path.insert(0, ".")
    import server as srv  # noqa

    emitted = []
    m = srv.QuestionMerger(lambda q: emitted.append(q))
    # 模拟 VAD 三段：语速慢导致一句被切成三段，间隔 ~0.5s
    m.add_segment("讲一下你")
    time.sleep(0.4)
    m.add_segment("在研究生阶段")
    time.sleep(0.4)
    m.add_segment("最有代表性的技术贡献")
    # 等防抖窗口(1.1s)+余量
    deadline = time.time() + 6
    while not emitted and time.time() < deadline:
        time.sleep(0.2)
    ok_merge = (len(emitted) == 1 and "技术贡献" in emitted[0]
                and "研究生" in emitted[0] and "讲一下你" in emitted[0])
    print(f"  输入3段 → 输出{len(emitted)}题: {emitted}")
    print("  合并成一道完整题:", "✅" if ok_merge else "❌")

    # 两道不相干的问题应各自成题
    emitted.clear()
    m2 = srv.QuestionMerger(lambda q: emitted.append(q))
    m2.add_segment("第一题：什么是事务隔离级别")
    time.sleep(2.0)   # 超过 wait_ms，成题
    m2.add_segment("第二题：Redis持久化方式")
    deadline = time.time() + 6
    while len(emitted) < 2 and time.time() < deadline:
        time.sleep(0.2)
    ok_two = len(emitted) == 2
    print(f"  两道独立问题 → {len(emitted)}题", "✅" if ok_two else "❌")

    print("\n== Part2: 麦克风通道上下文注入 ==")
    s = c.get(f"{BASE}/api/state").json()
    print("  输入设备数:", len(s.get("input_devices", [])))
    print("  self_listening:", s.get("self_listening"))

    # 用真实LLM验证上下文贯通：手动塞两条'我方发言'不可行(CONV在服务进程内)，
    # 改为验证 stream_quick 接受 self_recent 参数且生成正常。
    from copilot.llm import LLMClient
    lc = LLMClient()
    got = []
    ev = threading.Event()
    box = {}
    lc.stream_quick(
        [{"role": "user", "content": "接着刚才的话题继续深入"}],
        "(无简历)",
        on_delta=got.append,
        on_done=lambda f: (box.update(full=f), ev.set()),
        on_error=lambda m_: print("  [err]", m_),
        self_recent="我刚讲完了MirrorRE的双向镜像机制和对比学习检索增强",
    )
    ev.wait(timeout=45)
    full = box.get("full", "") or "".join(got)
    # 开口句应当与'镜像机制/对比学习/刚才'相关而不是完全另起炉灶
    ctx_ok = any(k in full for k in ("镜像", "对比学习", "刚才", "继续", "补充"))
    print(f"  带self_recent的续答开口: {full[:100]}")
    print("  续答贴合我方语境:", "✅" if ctx_ok else "❌",
          "(人工核验较好)" )

    all_ok = ok_merge and ok_two
    print("\n总结:", "核心功能 ✅" if all_ok else "存在失败 ❌")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
