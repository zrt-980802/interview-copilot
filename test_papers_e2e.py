# -*- coding: utf-8 -*-
"""论文知识库 E2E：提一个跨论文的问题，验证 AI 引用了知识库里的真实论文细节。"""
import json
import sys
import threading
import time

import httpx

BASE = "http://127.0.0.1:8527"
Q = "除了MirrorRE，你还有哪些在研的工作？讲讲它们分别解决什么问题"


def main():
    evs = []
    done = threading.Event()

    def listen():
        try:
            with httpx.Client(timeout=90).stream("GET", f"{BASE}/api/events") as resp:
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

    threading.Thread(target=listen, daemon=True).start()
    time.sleep(1.0)

    t0 = time.time()
    httpx.Client(timeout=10).post(f"{BASE}/api/ask", json={"text": Q})
    done.wait(timeout=70)
    parts = [e.get("delta", "") for e in evs if e.get("type") == "answer_delta"]
    dones = [e for e in evs if e.get("type") == "answer_done" and e.get("level") == "quick"]
    full = dones[0]["full"] if dones else "".join(parts)
    print(f"[{time.time()-t0:.1f}s]")
    print(full[:600])
    ok = all(k in full for k in ("GuideRE", "AoG")) or (
        "GuideRE" in full or ("关系抽取" in full and "schema" in full) and
        ("图" in full or "推理" in full))
    try:
        httpx.Client(timeout=5).post(f"{BASE}/api/clear")
    except Exception:
        pass
    print()
    print("引用知识库中的其他论文:", "✅" if ok else "❌")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
