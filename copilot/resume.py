# -*- coding: utf-8 -*-
"""简历模块：把候选人的真实简历解析入库，作为 AI 作答的"基础环境"(ground truth)。

职责：
- 支持 PDF / Markdown / TXT 三种格式；
- 清洗 pypdf 从中文字体模板提取出的图标伪影字符；
- 统一写入 data/resume.txt（AI 注入的唯一事实源），元数据写 data/resume_meta.json；
- bootstrap()：服务启动时的自动校准——优先级显式指定的路径 > 桌面常见简历 > 项目内 resume.md。
"""
import glob
import hashlib
import json
import os
import re
import time

from pypdf import PdfReader

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
RESUME_TXT = os.path.join(DATA_DIR, "resume.txt")
META_JSON = os.path.join(DATA_DIR, "resume_meta.json")

USER_HOME = os.path.expanduser("~")
DESKTOP = os.path.join(USER_HOME, "Desktop")
ONE_DRIVE_DESKTOP = os.path.join(USER_HOME, "OneDrive", "Desktop")

# pypdf 从中文简历模板(带图标字体)提取文本时的高频伪影符号
_ARTIFACTS_RE = re.compile(r"[ƟƢƣƤƥƦƧƧƩƪƫƬƭƮƯưƱƲƳƴƵƶƷƸƹƺƻƼƽƾƿǀǁǂǃǄǅǆǇǈǉǈǊǋǌǍǎǏǐǑǒǓǔǕǖǗǘǙǚǛǜǝǞǟǠǡǢǣǤǥǦǧǨǩǪǫǬǭǮǯǴǵǶǷǸǹǺǻǼǽǾǿȀȁȂȃȄȅȆȇȈȉȊȋȌȍȎȏȐȑȒȓȔȕȖȗȘșȚțȜȝȞȟȠȡȤȥȦȧȨȩȪȫȬȭȮȯȰȱȲȳȰȱ]\s*")


def extract_text(path: str) -> str:
    """按扩展名提取原始文本。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        reader = PdfReader(path)
        pages = []
        for p in reader.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n".join(pages)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def clean_text(raw: str) -> str:
    """去伪影、折叠空行与过长空白。"""
    t = _ARTIFACTS_RE.sub(" ", raw)
    t = t.replace("\u00a0", " ")
    # 行内长串空白折叠
    lines = []
    for ln in t.splitlines():
        ln2 = re.sub(r"[ \t]{3,}", "  ", ln).rstrip()
        if ln2.strip():
            lines.append(ln2)
    return "\n".join(lines).strip()


def _meta(origin_path: str, chars: int, note: str = "") -> dict:
    try:
        with open(RESUME_TXT, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:10]
    except Exception:
        digest = ""
    try:
        mtime = int(os.path.getmtime(origin_path))
    except OSError:
        mtime = 0
    return {
        "loaded": chars > 0,
        "source": os.path.basename(origin_path),
        "origin": origin_path,
        "chars": chars,
        "sha256_10": digest,
        "source_mtime": mtime,
        "loaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
    }


def ingest(path: str, label: str = "", *, source_name: str | None = None) -> dict:
    """解析指定文件并入库。失败抛异常。"""
    raw = extract_text(path)
    text = clean_text(raw)
    if len(text) < 50:
        raise ValueError(f"提取到的内容过少({len(text)}字符)，可能不是有效简历或为扫描件")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RESUME_TXT, "w", encoding="utf-8") as f:
        f.write(text)
    meta = _meta(path, len(text), label)
    if source_name:
        meta["source"] = source_name
    with open(META_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def clear():
    """清除已入库简历。"""
    for p in (RESUME_TXT, META_JSON):
        try:
            os.remove(p)
        except OSError:
            pass


def current():
    """返回 (当前生效的简历全文, meta或None)。"""
    try:
        with open(RESUME_TXT, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return "", None
    meta = None
    try:
        with open(META_JSON, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["loaded"] = bool(text)
    except Exception:
        meta = {"loaded": bool(text), "source": "?"}
    return text, meta


def find_desktop_resume() -> str | None:
    """在桌面(含OneDrive桌面)找最合适的简历 PDF：resume*.pdf / *简历*.pdf 中取最新。"""
    patterns = ["resume*.pdf", "*简历*.pdf", "*CV*.pdf"]
    cands = []
    for root in {DESKTOP, ONE_DRIVE_DESKTOP}:
        for pat in patterns:
            for hit in glob.glob(os.path.join(root, pat)):
                if os.path.isfile(hit):
                    cands.append(hit)
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def bootstrap(explicit_path: str | None = None) -> dict | None:
    """启动自动校准：已有入库内容时检查源文件是否更新过，更新则自动重解析；
    否则按 显式路径 > 桌面最新简历 > 项目内 resume.md 的顺序加载。返回meta或None。"""
    text, meta = current()
    if text and meta and meta.get("origin") and os.path.isfile(meta["origin"]):
        try:
            if int(os.path.getmtime(meta["origin"])) == int(meta.get("source_mtime") or 0):
                return meta          # 源文件没变，直接复用
        except OSError:
            pass                     # 取不到mtime就走重载
    candidates = []
    if explicit_path and os.path.isfile(explicit_path):
        candidates.append((explicit_path, "config 指定"))
    desk = find_desktop_resume()
    if desk:
        candidates.append((desk, "桌面自动发现"))
    md = os.path.join(BASE_DIR, "resume.md")
    if os.path.isfile(md):
        candidates.append((md, "项目内置 resume.md"))
    for path, label in candidates:
        try:
            return ingest(path, label)
        except Exception:
            continue
    return None
