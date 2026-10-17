#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/make-portable.py — 把「网页 + hub」打包成自包含文件夹，方便放进板子的 TF 卡带走。

为什么要它：现在的网页是这台 PC 上的 hub（:8765）在托管；换一台电脑就没有了。
板子本身没有网络出口（不插网线、C6 Wi-Fi 方案已放弃），固件也只有
sd_test/sd_remount/sd_format 三个卡命令、**没有文件读写命令**，所以 PC
没法直接把文件写进板子里的 TF 卡、也没法让板子自己当 Web 服务器。

可行做法：打一个**不依赖本仓库**的小包（hub + web，纯标准库），
放到 TF 卡 / U 盘 / 网盘上；换电脑时把整个文件夹拷出来，双击 `start-dashboard.bat`
就能起同样的仪表盘（新电脑只要有 Python 3.9+，不需要装任何第三方包）。

用法：
    python tools\\make-portable.py                          # 生成到 <workspace>\\portable-esp32p4-web
    python tools\\make-portable.py --out D:\\site           # 指定输出目录
    python tools\\make-portable.py --to-drive E:            # 直接拷到已插入的卡/U盘根目录
    python tools\\make-portable.py --zip                    # 额外打一个 zip

注意：--to-drive 只复制、不删除；目标盘上已有同名文件夹会合并覆盖同名文件。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                       # 仓库根
_WORKSPACE = os.path.dirname(_ROOT)
FOLDER_NAME = "portable-esp32p4-web"

FILES = [
    ("pc/hub.py", "pc/hub.py"),
    ("pc/transport_uart.py", "pc/transport_uart.py"),
    ("pc/transport_base.py", "pc/transport_base.py"),
    ("tools/board_lock.py", "tools/board_lock.py"),
    ("web/index.html", "web/index.html"),
    ("web/style.css", "web/style.css"),
    ("web/app.js", "web/app.js"),
    ("web/favicon.svg", "web/favicon.svg"),
]

BAT_START = r"""@echo off
setlocal
cd /d "%~dp0"
set PY=python
%PY% -c "import sys" >nul 2>nul || set PY=py -3
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python 3 not found. Install it from https://www.python.org/downloads/
  echo         During install, tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)
echo Starting dashboard on http://127.0.0.1:8765/ ...
start "" cmd /c "timeout /t 2 >nul & start "" http://127.0.0.1:8765/"
%PY% pc\hub.py --serial auto
echo.
echo Hub exited. Press any key to close.
pause >nul
"""

BAT_STOP = r"""@echo off
setlocal
set PY=python
%PY% -c "import sys" >nul 2>nul || set PY=py -3
powershell -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/shutdown -TimeoutSec 5).Content}catch{Write-Host 'hub is not running / already stopped'}"
echo.
pause >nul
"""

README = """ESP32-P4 仪表盘 · 便携包
============================

这是什么
  一个自包含的小网页服务：把这块 ESP32-P4 板子的状态（CPU/内存/TF卡/温度/调频/HID）
  显示成实时仪表盘。只用到 Python 标准库，不需要联网、不需要装第三方包。

怎么用（换电脑也一样）
  1. 板子两个口都插上：转 UART 口（CH340）+ 高速 Type-C 口（HID）。
  2. 双击 start-dashboard.bat。
  3. 浏览器会自动打开 http://127.0.0.1:8765/ 。手机/平板同 Wi-Fi 可开
     http://<这台电脑的 IP>:8765/ 。
  4. 关闭：双击 stop-dashboard.bat，或在那个黑窗口按 Ctrl+C。

换电脑要注意
  * 新电脑需要 Python 3.9+（安装时勾选 Add python.exe to PATH）。
  * COM 口号可能变：启动脚本用 --serial auto 自动识别 CH340，不用手改。
  * 端口 8765 被占用时：python pc\\hub.py --serial auto --port 8766

为什么不用「板子自己当服务器」
  板子没有网口，C6 Wi-Fi 方案已放弃；固件只有 sd_test/sd_remount/sd_format，
  没有文件读写命令，所以 PC 既不能往板子的 TF 卡里写文件，板子也没法自己发网页。
  这个便携包 = 把「服务端」装进 TF 卡/U 盘，插到哪台电脑都能起。

放哪儿
  整个文件夹拷到板子的 TF 卡（需要用读卡器把卡插到电脑）或 U 盘根目录即可。
  卡里文件名尽量保持英文，避免某些系统中文乱码。

自检
  python pc\\hub.py --serial COM99 --no-lock --port 8799 --tcp-port 0
  然后另开一个 PowerShell 窗口：
  (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/api/health).Content
  返回 {"ok":true,...,"board":{"state":"offline"...}} 就说明包是完整的。
"""


def build(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    copied = []
    for src_rel, dst_rel in FILES:
        src = os.path.join(_ROOT, src_rel)
        if not os.path.isfile(src):
            raise SystemExit("缺文件: %s" % src)
        dst = os.path.join(out_dir, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst_rel)

    with open(os.path.join(out_dir, "start-dashboard.bat"), "w", encoding="ascii",
              newline="\r\n") as f:
        f.write(BAT_START)
    with open(os.path.join(out_dir, "stop-dashboard.bat"), "w", encoding="ascii",
              newline="\r\n") as f:
        f.write(BAT_STOP)
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8-sig",
              newline="\r\n") as f:
        f.write(README)

    total = 0
    for base, _dirs, files in os.walk(out_dir):
        for fn in files:
            total += os.path.getsize(os.path.join(base, fn))
    return {"ok": True, "dir": out_dir, "files": len(copied) + 3, "bytes": total}


def make_zip(out_dir: str) -> str:
    zip_path = out_dir.rstrip("\\/") + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(out_dir):
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, out_dir)
                z.write(full, os.path.join(FOLDER_NAME, rel))
    return zip_path


def copy_to_drive(drive: str, src_dir: str) -> dict:
    drive = drive.strip()
    if len(drive) == 1:
        drive += ":"
    root = drive.rstrip("\\/") + "\\"
    if not os.path.isdir(root):
        raise SystemExit("目标盘不存在: %s（插好读卡器/U 盘后确认盘符）" % root)
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise SystemExit("读不到目标盘 %s：%r" % (root, exc))
    need = sum(os.path.getsize(os.path.join(b, f))
               for b, _d, fs in os.walk(src_dir) for f in fs)
    if free < need + 1024 * 1024:
        raise SystemExit("目标盘空间不够：需要 %.1f MB，剩 %.1f MB"
                         % (need / 1e6, free / 1e6))
    dst = os.path.join(root, FOLDER_NAME)
    shutil.copytree(src_dir, dst, dirs_exist_ok=True)
    return {"ok": True, "copied_to": dst, "bytes": need, "free_before_mb": round(free / 1e6, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打一个可放进 TF 卡/U 盘的便携网页包")
    ap.add_argument("--out", default=os.path.join(_WORKSPACE, FOLDER_NAME),
                    help="输出目录（默认 <workspace>\\%s）" % FOLDER_NAME)
    ap.add_argument("--to-drive", default=None, help="直接拷到该盘符根目录，如 E:")
    ap.add_argument("--zip", action="store_true", help="额外生成 zip")
    a = ap.parse_args(argv)

    info = build(a.out)
    if a.zip:
        info["zip"] = make_zip(a.out)
        info["zip_bytes"] = os.path.getsize(info["zip"])
    if a.to_drive:
        info["drive"] = copy_to_drive(a.to_drive, a.out)
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
