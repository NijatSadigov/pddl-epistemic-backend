#!/usr/bin/env python3
"""Tiny, dependency-free HTTP API around pdkb-planning (RP-MEP).

Endpoints
  GET  /health  -> {"ok": true}
  POST /solve   {"pdkbddl": "<problem text>"}
                -> {"ok": bool, "plan": [...], "output": "<raw planner output>",
                    "returncode": int, "error"?: str}

Safety model (important on a small / shared host):
  * The server is SINGLE-THREADED, so solves run one at a time — a natural queue
    that prevents several Fast Downward processes from multiplying memory.
  * Each solve runs in its own process group with an address-space rlimit
    (SOLVE_MEM_MB) and a wall-clock timeout (SOLVE_TIMEOUT). On timeout the whole
    group is killed, so the bundled planner can't linger.
  * Pair this with a hard container memory cap (docker run --memory=…); because
    each solve is capped below that, it is the SOLVE that dies under pressure,
    not this server or the rest of the host.
"""

import json
import os
import re
import resource
import signal
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "8000"))
SOLVE_TIMEOUT = int(os.environ.get("SOLVE_TIMEOUT", "30"))
SOLVE_MEM_MB = int(os.environ.get("SOLVE_MEM_MB", "600"))
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")
MAX_BODY = 256 * 1024  # reject inputs larger than 256 KB


def _limit_memory():
    """preexec_fn: cap the solve subtree's virtual address space."""
    soft = SOLVE_MEM_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (soft, soft))


def parse_plan(output):
    """Best-effort plan extraction.

    `pdkb.planner` prints the plan after a `--{ Plan }--` banner, one numbered
    step per line: `1. (move_c_l1_l2)`. We scope to that section when present and
    grab each action, tolerating the leading number; otherwise we fall back to
    any bare parenthesised action lines. The full `output` is always returned
    too, so nothing is lost if this misses.
    """
    marker = re.search(r"--\{\s*Plan\s*\}--", output)
    section = output[marker.end():] if marker else output
    actions = re.findall(r"^\s*(?:\d+\.\s*)?\(([^()\n]+)\)\s*$", section, re.MULTILINE)
    return ["(%s)" % a.strip() for a in actions]


def run_planner(pdkbddl_text):
    workdir = tempfile.mkdtemp(prefix="solve-")
    path = os.path.join(workdir, "problem.pdkbddl")
    with open(path, "w") as fh:
        fh.write(pdkbddl_text)

    try:
        proc = subprocess.Popen(
            ["python3", "-m", "pdkb.planner", path],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=_limit_memory,
            start_new_session=True,  # own group, so we can kill Fast Downward too
        )
        try:
            out, _ = proc.communicate(timeout=SOLVE_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.communicate()
            return {
                "ok": False,
                "error": "timeout",
                "output": "Solve exceeded %d s and was stopped." % SOLVE_TIMEOUT,
            }
        output = out.decode("utf-8", "replace")
        rc = proc.returncode
    finally:
        try:
            os.remove(path)
            os.rmdir(workdir)
        except OSError:
            pass

    plan = parse_plan(output)
    return {
        "ok": rc == 0,
        "returncode": rc,
        "plan": plan,
        "output": output[-20000:],  # cap response size
    }


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/solve":
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._json(
                413,
                {"ok": False, "error": "missing or too-large body (max %d bytes)" % MAX_BODY},
            )
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
            text = data["pdkbddl"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError
        except Exception:
            return self._json(
                400, {"ok": False, "error": 'expected JSON {"pdkbddl": "<problem text>"}'}
            )
        try:
            result = run_planner(text)
        except MemoryError:
            result = {"ok": False, "error": "out of memory"}
        except Exception as exc:  # never crash the server on a bad solve
            result = {"ok": False, "error": "solver error: %s" % exc}
        self._json(200, result)

    def log_message(self, fmt, *args):
        # one terse line per request
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    print(
        "epistemic solver API on :%d  (timeout=%ds, mem=%dMB, cors=%s)"
        % (PORT, SOLVE_TIMEOUT, SOLVE_MEM_MB, CORS_ORIGIN)
    )
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
