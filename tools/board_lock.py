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
    return {"locked": age < STALE_SECONDS, "age_s": round(age, 1), **info}


def acquire(who, timeout=600.0, poll=1.0):
    deadline = time.time() + timeout
    while True:
        info = _read()
        if info:
            age = time.time() - info.get("ts", 0)
            if age < STALE_SECONDS:
                if time.time() > deadline:
                    raise TimeoutError(
                        "board lock held by %s for %.0fs" % (info.get("who"), age))
                time.sleep(poll)
                continue
            print("[board_lock] breaking stale lock of %s (%.0fs old)" % (info.get("who"), age),
                  file=sys.stderr)
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass
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
