# -*- coding: utf-8 -*-
"""简历接地(E2E)：验证 AI 作答引用真实简历。
模拟真实场景：桌面 Chrome(已连SSE) + 本脚本 同时监听 —— 两者都应拿到完整卡片。
"""
import json
import sys
import threading
import time

import httpx

BASE = "http://127.0.0.1:8527"
Q = "请介绍一下你的多模态关系抽取研究"


def listen(tag, results):
    parts = []
    try:
        with httpx.Client(timeout=100).stream("GET", f"{BASE}/api/events") as resp:
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                if ev.get("type") == "answer_delta":
                    parts.append(ev.get("delta", ""))
                elif ev.get("type") == "answer_done":
                    break
    except Exception as e:
        print(f"[{tag}] 监听异常: {e}", flush=True)
    results[tag] = "".join(parts)
    print(f"[{tag}] 收到 {len(results[tag])} 字", flush=True)


def main():
    c = httpx.Client(timeout=15)
    s = c.get(f"{BASE}/api/state").json()
    print("简历状态:", json.dumps(s["resume"], ensure_ascii=False), flush=True)

    results = {}
    t1 = threading.Thread(target=listen, args=("client-A(模拟Chrome)", results), daemon=True)
    t1.start()
    time.sleep(0.8)   # 错开连接，验证广播不互抢
    t2 = threading.Thread(target=listen, args=("client-B(本测试)", results), daemon=True)
    t2.start()
    time.sleep(0.8)

    t0 = time.time()
    c.post(f"{BASE}/api/ask", json={"text": Q})
    t1.join(timeout=90)
    t2.join(timeout=5)

    full = results.get("client-B(本测试)", "")
    print(f"\n耗时 {time.time()-t0:.1f}s")
    print("=== 作答卡 ===")
    print(full[:480])
    grounded = any(k in full for k in ("EIBDCT", "ERE", "Micro-F1", "知识图谱", "RAG",
                                       "BNBU", "双镜像", "对比学习"))
    both_ok = len(results.get("client-A(模拟Chrome)", "")) > 50 and len(full) > 50
    print()
    print("广播双客户端完整:", "✅" if both_ok else "❌")
    print("引用真实简历内容:", "是 ✅" if grounded else "否 ❌")
    sys.exit(0 if (grounded and both_ok) else 1)


if __name__ == "__main__":
    main()
