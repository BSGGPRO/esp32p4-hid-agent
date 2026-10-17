"""Cross-process lock for the single ESP32-P4 board (COM7).

The board exposes exactly one UART command channel, so firmware flashing,
protocol sessions and drawing scripts must take turns. Every script/agent that
touches COM7 must hold this lock for the whole serial session.

Usage (CLI):
    python tools/board_lock.py acquire --who "fw-core:flash" [--timeout 600]
    python tools/board_lock.py release --who "fw-core:flash"
    python tools/board_lock.py status

Usage (library):
    from board_lock import BoardLock
    with BoardLock("draw:rage"):
        ...  # open COM7, talk, close
"""
import argparse
import json
import os
import sys
import time

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".board.lock")
LOCK_PATH = os.path.abspath(LOCK_PATH)
STALE_SECONDS = 900
# 持有者进程已死时，等这么久（秒）就判定为僵尸锁并立刻抢占，不用等 900 s
DEAD_PID_GRACE = 3.0


def _pid_alive(pid):
    """判断 pid 是否还活着（Windows 用 OpenProcess，避免 os.kill 的杀进程语义）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # 查不到就当活着，宁可保守
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_stale(info, age):
    """锁是否可被抢占：超过 STALE_SECONDS，或持有者进程已死（宽限 3 s）。"""
    if age >= STALE_SECONDS:
        return True, "%.0fs old" % age
    host = (os.environ.get("COMPUTERNAME") or "").lower()
    lock_host = (info.get("host") or "").lower()
    if host and lock_host and host != lock_host:
        return False, ""
    if age > DEAD_PID_GRACE and not _pid_alive(info.get("pid")):
        return True, "holder pid %s is dead" % info.get("pid")
    return False, ""


def _read():
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def status():
    info = _read()
    if not info:
        return {"locked": False}
    age = time.time() - info.get("ts", 0)
    stale, why = _is_stale(info, age)
    return {"locked": not stale, "stale": stale, "stale_reason": why,
            "age_s": round(age, 1), "holder_alive": _pid_alive(info.get("pid")), **info}


def acquire(who, timeout=600.0, poll=1.0):
    deadline = time.time() + timeout
    while True:
        info = _read()
        if info:
            age = time.time() - info.get("ts", 0)
            stale, why = _is_stale(info, age)
            if stale:
                print("[board_lock] breaking stale lock of %s (%s)" % (info.get("who"), why),
                      file=sys.stderr)
                try:
                    os.remove(LOCK_PATH)
                except OSError:
                    pass
            else:
                if time.time() > deadline:
                    raise TimeoutError(
                        "board lock held by %s for %.0fs" % (info.get("who"), age))
                time.sleep(poll)
                continue
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            time.sleep(poll)
            continue
        payload = {"who": who, "pid": os.getpid(), "ts": time.time(),
                   "host": os.environ.get("COMPUTERNAME", "")}
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return payload


def release(who=None):
    info = _read()
    if info and who and info.get("who") != who:
        print("[board_lock] not owner (%s vs %s), leaving lock" % (info.get("who"), who),
              file=sys.stderr)
        return False
    try:
        os.remove(LOCK_PATH)
        return True
    except OSError:
        return False


class BoardLock:
    def __init__(self, who, timeout=600.0):
        self.who = who
        self.timeout = timeout

    def __enter__(self):
        acquire(self.who, self.timeout)
        return self

    def __exit__(self, *exc):
        release(self.who)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["acquire", "release", "status", "touch"])
    ap.add_argument("--who", default="unknown")
    ap.add_argument("--timeout", type=float, default=600.0)
    a = ap.parse_args()
    if a.action == "acquire":
        print(json.dumps(acquire(a.who, a.timeout), ensure_ascii=False))
    elif a.action == "release":
        print(json.dumps({"released": release(a.who)}))
    elif a.action == "touch":
        info = _read()
        if info and info.get("who") == a.who:
            info["ts"] = time.time()
            with open(LOCK_PATH, "w", encoding="utf-8") as f:
                json.dump(info, f)
            print(json.dumps(info))
        else:
            print(json.dumps({"error": "not owner"}))
    else:
        print(json.dumps(status(), ensure_ascii=False))


if __name__ == "__main__":
    main()
