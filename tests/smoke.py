"""冒烟测试：只测不需要板子、不需要 OCR/VLM 模型的纯逻辑。

跑法：`python tests/smoke.py`（CI 上也是这一条）。
需要板子的真机回归不在 CI 里跑——那套要点鼠标、看屏幕，只在真机上做。
"""
from __future__ import annotations

import compileall
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra and not cond else ""))
    if not cond:
        failures.append(name)


# 1) 所有 Python 文件语法正确（CI 最有价值的一步：防止推上去的代码根本跑不起来）
ok = compileall.compile_dir(str(ROOT / "pc"), quiet=2)
ok = compileall.compile_dir(str(ROOT / "tools"), quiet=2) and ok
check("全部 .py 能编译", ok)

# 2) 导入连接器（Windows 专用：ctypes 直调 user32/kernel32，所以 CI 用 windows runner）
import connector as C  # noqa: E402

check("alnum 去掉标点", C.alnum("Deep Seek, 你好!") == "deepseek你好",
      C.alnum("Deep Seek, 你好!"))
check("ratio 认近似匹配", C.ratio("文件传输助手", "文件 传输助手") > 0.99,
      str(C.ratio("文件传输助手", "文件 传输助手")))
check("ratio 不认无关文本", C.ratio("文件传输助手", "微信支付") < 0.3,
      str(C.ratio("文件传输助手", "微信支付")))

# 3) poll：立刻满足 / 超时 / 第三次满足
import time  # noqa: E402

t = time.time()
check("poll 立刻返回", C.poll(lambda: True, 2) is True and time.time() - t < 0.5)
check("poll 超时返回 None", C.poll(lambda: False, 0.3, 0.05) is None)
n = [0]


def third():
    n[0] += 1
    return n[0] >= 3


t = time.time()
check("poll 满足即停", C.poll(third, 3, 0.05) is True and time.time() - t < 1.0)

# 4) _yesno：否定词优先（「不是」里也有「是」）
check("_yesno 是", C._yesno("是") is True)
check("_yesno 不是", C._yesno("不是") is False)
check("_yesno 判不了返回 None", C._yesno("可能吧") is None)

# 5) 找块：一张牌子上画两个白方块，应该找到两块，且位置对得上
#    （亮块定位要 numpy；只装 Pillow 的环境里跳过，装了就真测）
from PIL import Image, ImageDraw  # noqa: E402

try:
    import numpy  # noqa: F401
    has_numpy = True
except ImportError:
    has_numpy = False

if not has_numpy:
    print("SKIP  亮块定位（没装 numpy；ui 的按钮定位需要它，OCR 那套装了就有）")
else:
    im = Image.new("RGB", (400, 200), (20, 20, 20))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 60, 80, 100), fill=(240, 240, 240))     # 中心 (60, 80)
    d.rectangle((300, 60, 340, 100), fill=(240, 240, 240))   # 中心 (320, 80)
    blocks = [b for b in C._blobs(im) if 10 <= b[2] <= 90 and 10 <= b[3] <= 90]
    check("找到两个块", len(blocks) == 2, str(blocks))
    if len(blocks) == 2:
        blocks.sort()
        cx = [b[0] + b[2] // 2 for b in blocks]
        check("块位置对得上", abs(cx[0] - 60) <= 6 and abs(cx[1] - 320) <= 6, str(cx))

    check("纯色图判为空白", C._blank(Image.new("RGB", (200, 120), (25, 25, 25))) is True)
    check("有内容不算空白", C._blank(im) is False)

# 6) 文档里的操作数要对得上代码（防止 README 和实现各说各话）
#    用 AST 数 run_job 里认的操作：既认 op == "x"，也认 op in ("x", "y") 这种元组匹配。
import ast  # noqa: E402
import re  # noqa: E402


def ops_in_code() -> set[str]:
    tree = ast.parse((ROOT / "tools" / "connector.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_job")
    found: set[str] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "op"):
            continue
        for comp in node.comparators:
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                found.add(comp.value)
            elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                found |= {e.value for e in comp.elts
                          if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return found


ops = ops_in_code()
for path, pattern, label in (
    (ROOT / "README.md", r"一个文件，(\d+) 个操作", "README.md"),
    (ROOT / "README.en.md", r"one file, (\d+) operations", "README.en.md"),
):
    m = re.search(pattern, path.read_text(encoding="utf-8"))
    check("%s 操作数与代码一致" % label, bool(m) and int(m.group(1)) == len(ops),
          "文档=%s 代码=%d %s" % (m.group(1) if m else "?", len(ops), sorted(ops)))

print("-" * 60)
if failures:
    print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("全部通过")
