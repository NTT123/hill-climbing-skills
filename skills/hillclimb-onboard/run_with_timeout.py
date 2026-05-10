#!/usr/bin/env python3
"""run_with_timeout.py <seconds> <command...>

Run <command> in a new process group; on timeout, SIGTERM then SIGKILL after
GRACE seconds. Exits with the command's exit code, or 124 if killed for
timing out (matches GNU `timeout`).
"""

import os
import signal
import subprocess
import sys

GRACE = 5


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write("usage: run_with_timeout.py <seconds> <command...>\n")
        return 2
    try:
        timeout_s = float(sys.argv[1])
    except ValueError:
        sys.stderr.write(f"bad timeout: {sys.argv[1]}\n")
        return 2

    p = subprocess.Popen(sys.argv[2:], start_new_session=True)
    try:
        return p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        p.wait(timeout=GRACE)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()

    sys.stderr.write(f"killed after {timeout_s}s\n")
    return 124


if __name__ == "__main__":
    sys.exit(main())
