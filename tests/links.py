"""检查 Markdown 里的相对链接都指向真实存在的文件。

跑法：`python tests/links.py`。CI 上用这一条。
为什么单独写：原来在 CI 里用 `grep | while read` 实现，管道里的 while 是子 shell，
`fail=1` 传不出去，脚本永远退出 0——检查形同虚设。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "README.md", ROOT / "README.en.md", *sorted((ROOT / "docs").glob("*.md"))]

LINK = re.compile(r"\]\(([^)]+)\)")


def main() -> int:
    bad = 0
    for f in FILES:
        if not f.exists():
            print("缺文件: %s" % f.relative_to(ROOT))
            bad += 1
            continue
        for raw in LINK.findall(f.read_text(encoding="utf-8")):
            link = raw.split()[0].strip("<>")          # [文字](路径 "可选标题")
            if link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = link.split("#")[0]
            if not target:
                continue                            # 纯锚点
            for base in (f.parent, ROOT):           # 相对本文档，或相对仓库根
                if (base / target).exists():
                    break
            else:
                print("坏链接: %s → %s" % (f.relative_to(ROOT), link))
                bad += 1
    if bad:
        print("共 %d 个问题" % bad)
        return 1
    print("链接全部有效（%d 个文件）" % len(FILES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
