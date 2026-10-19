# -*- coding: utf-8 -*-
"""两级作答 E2E：
1) 提问 -> 快速卡（应含【开口】样板句 + 【方向】）
2) 请求深挖某方向 -> 子卡（应含深挖细节/追问预备）
3) 请求'完整版' -> SAR 结构
"""
import json
import sys
import threading
import time

import httpx

BASE = "http://127.0.0.1:8527"
Q = "讲一下你最有代表性的研究成果"


def main():
    events = []
    done_quick = threading.Event()
    ev_lock = threading.Lock()

    def listen():
        try:
            with httpx.Client(timeout=120).stream("GET", f"{BASE}/api/events") as resp:
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    with ev_lock:
                        events.append(ev)
                        if ev.get("type") == "answer_done" and ev.get("level") == "quick":
                            done_quick.set()
        except Exception as e:
            print("[listen]", e, flush=True)

    threading.Thread(target=listen, daemon=True).start()
    time.sleep(1.0)

    c = httpx.Client(timeout=15)
    t0 = time.time()
    c.post(f"{BASE}/api/ask", json={"text": Q})
    ok = done_quick.wait(timeout=60)
    quick_full = ""
    dirs = []
    if ok:
        for ev in events:
            if ev.get("type") == "answer_done" and ev.get("level") == "quick":
                quick_full = ev.get("full", "")
                break
    else:
        # 兜底：从事件里拼增量
        parts = [ev.get("delta", "") for ev in events if ev.get("type") == "answer_delta"]
        quick_full = "".join(parts)
    dt = time.time() - t0

    has_opener = "【开口】" in quick_full
    has_dir = "【方向】" in quick_full
    print(f"[{dt:.1f}s] 快速卡原文:")
    print(quick_full[:400])
    print()

    # 解析出第一个方向名来点深挖
    dir_name = "完整版"
    for ln in quick_full.splitlines():
        if ln.startswith("【方向】"):
            seg = ln[4:].split(":::")
            dir_name = (seg[0] or "").strip() or dir_name
            break

    # 深挖
    n_before = len(events)
    c.post(f"{BASE}/api/detail", json={"direction": dir_name})
    time.sleep(14)
    detail_parts = []
    got_detail_done = False
    for ev in events[n_before:]:
        if ev.get("type") == "answer_delta":
            detail_parts.append(ev.get("delta", ""))
        elif ev.get("type") == "answer_done" and ev.get("level") == "detail":
            detail_parts = [ev.get("full", "")]  # 用完整版
            got_detail_done = True
            break
    detail_text = "".join(detail_parts)
    print(f"[深挖方向: {dir_name}]")
    print(detail_text[:350])
    print()

    checks = [
        ("快速卡含开口样板句【开口】", has_opener),
        ("快速卡含展开方向【方向】", has_dir),
        ("快速卡耗时<20s", dt < 20),
        ("深挖返回非空且含关键词", len(detail_text) > 40 and
         any(k in detail_text for k in ("MirrorRE", "对比学习", "EIBDCT", "追问", "镜像"))),
    ]
    all_ok = True
    for name, passed in checks:
        print(("✅" if passed else "❌"), name)
        all_ok &= bool(passed)

    # 清理上下文，避免影响后续手动使用
    try:
        c.post(f"{BASE}/api/clear")
    except Exception:
        pass
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
