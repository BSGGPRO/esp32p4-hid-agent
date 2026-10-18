"""检查 Markdown 里的相对链接都指向真实存在的文件。

跑法：`python tests/links.py`。CI 上用这一条。

两个刻意的选择：
1. 只认 **git 跟踪** 的文件。原来按文件系统判断，本机存在的（比如被 gitignore 的
   docs/evidence/ 截图）就算通过，CI 上一克隆全断——本地绿、线上红，检查等于骗自己。
2. 强制 UTF-8 输出：Windows 控制台默认 cp1252，打中文会抛 UnicodeEncodeError。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "README.md", ROOT / "README.en.md", *sorted((ROOT / "docs").glob("*.md"))]

LINK = re.compile(r"\]\(([^)]+)\)")
# 反引号里的路径也会撒谎：operations.md 曾经指向一个根本不存在的 HANDOFF-TO-AI.txt。
TICK = re.compile(r"`([^`\n]+)`")
CHECKED_EXT = (".md", ".txt", ".py", ".json", ".cmd", ".yml", ".yaml", ".png", ".gif")
TOP_DIRS = {"docs", "tools", "pc", "web", "firmware", "tests", ".github"}


def tick_targets(text: str) -> list[str]:
    """挑出反引号里当**仓库路径**写的引用，比如 ``docs/x.md``、``tools/y.py``。

    刻意收紧，否则全是噪声：散文中提到的文件名（`manual.txt`、`plan.json`）、
    故意不入库的证据目录（docs/evidence/）、Windows 反斜杠路径、通配符，一律跳过。
    """
    out = []
    for tok in TICK.findall(text):
        tok = tok.strip()
        if not tok or " " in tok or "\\" in tok:
            continue
        if any(c in tok for c in "*?<>%") or "://" in tok or tok.endswith("/"):
            continue
        if "/" not in tok or tok.split("/")[0] not in TOP_DIRS:
            continue
        if tok.startswith("docs/evidence/"):
            continue                       # 按设计不入库，文档里已写明留在那台机器上
        if not tok.lower().endswith(CHECKED_EXT):
            continue
        out.append(tok)
    return out


def tracked() -> set[str] | None:
    """仓库里被 git 跟踪的文件（相对仓库根）。拿不到 git 就返回 None，退回文件系统判断。"""
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                           capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def main() -> int:
    files = tracked()
    bad = 0
    if files is None:
        print("提醒：拿不到 git 文件列表，退回按文件系统判断（本地存在但没入库的会漏）")
    for f in FILES:
        rel = f.relative_to(ROOT).as_posix()
        if not f.exists():
            print("缺文件: %s" % rel)
            bad += 1
            continue
        for raw in LINK.findall(f.read_text(encoding="utf-8")):
            link = raw.split()[0].strip("<>")          # [文字](路径 "可选标题")
            if link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = link.split("#")[0]
            if not target:
                continue                            # 纯锚点
            if files is not None:
                # 相对本文档所在目录，或相对仓库根
                cands = [(f.parent / target).relative_to(ROOT).as_posix(), target]
                if any(c in files for c in cands):
                    continue
                print("坏链接: %s → %s（git 里没有这个文件）" % (rel, link))
                bad += 1
                continue
            for base in (f.parent, ROOT):
                if (base / target).exists():
                    break
            else:
                print("坏链接: %s → %s" % (rel, link))
                bad += 1

        # 第二遍：反引号里当路径写的引用
        text = f.read_text(encoding="utf-8")
        for tok in tick_targets(text):
            cands = [(f.parent / tok).relative_to(ROOT).as_posix(), tok]
            if files is not None:
                ok = any(c in files for c in cands)
            else:
                ok = any((b / tok).exists() for b in (f.parent, ROOT))
            if not ok:
                print("坏引用: %s → `%s`（git 里没有这个文件）" % (rel, tok))
                bad += 1
    if bad:
        print("共 %d 个问题" % bad)
        return 1
    print("链接全部有效（%d 个文件%s）" % (len(FILES), "，按 git 跟踪文件核对" if files else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
