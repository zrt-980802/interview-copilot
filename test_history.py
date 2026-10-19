# -*- coding: utf-8 -*-
"""持久化 E2E：问一题→深挖一次→查 sessions/turns/details→导出 Markdown。"""
import json
import sys
import threading
import time

import httpx

BASE = "http://127.0.0.1:8527"


def main():
    evs = []
    done = threading.Event()

    def listen():
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
                    if ev.get("type") == "answer_done" and ev.get("level") == "detail":
                        done.set()
                        return
        except Exception:
            pass

    threading.Thread(target=listen, daemon=True).start()
    time.sleep(1)

    c = httpx.Client(timeout=10)
    c.post(f"{BASE}/api/ask", json={"text": "介绍一下你的MirrorRE论文"})
    done.wait(timeout=60)   # 等 detail 完成（需要先等 quick，再发 detail）
    # 兜底：如果只等到 quick 就手动触发 deep
    had_detail_done = any(e.get("type") == "answer_done" and e.get("level") == "detail" for e in evs)
    if not had_detail_done:
        time.sleep(2)
        c.post(f"{BASE}/api/detail", json={"direction": "完整版"})
        done.wait(timeout=50)

    time.sleep(1.5)   # 给写线程 flush 时间
    r = c.get(f"{BASE}/api/history/sessions").json()
    sessions = r["sessions"]
    print(f"共 {len(sessions)} 个会话; 最新:")
    cur = sessions[0]
    print("  ", cur)

    det = c.get(f"{BASE}/api/history/session/{cur['id']}").json()["session"]
    print(f"  该会话 {len(det['turns'])} 轮")
    ok_turns = len(det["turns"]) >= 1
    t0turn = det["turns"][0]
    ok_quick = bool(t0turn["quick_card"])
    ok_details = any(len(t["details"]) > 0 for t in det["turns"])
    print("  turn问题:", t0turn["question"][:40])
    print("  快速卡落库:", "✅" if ok_quick else "❌", "| 深挖落库:", "✅" if ok_details else "❌")

    exp = c.get(f"{BASE}/api/history/export/{cur['id']}")
    md = exp.text
    ok_export = ("# 面试复盘" in md and "快速卡" in md)
    save_path = exp.headers.get("X-Save-Path", "")
    print("  Markdown导出:", "✅" if ok_export else "❌", "->", save_path)

    all_ok = ok_turns and ok_quick and ok_details and ok_export
    print("\n总结:", "持久化全链路 ✅" if all_ok else "❌")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
