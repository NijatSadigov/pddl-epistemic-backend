#!/usr/bin/env python3
"""Dependency-free HTTP API around pdkb-planning (RP-MEP) and its bundled
classical planner.

Endpoints
  GET  /health           -> {"ok": true}
  POST /solve            {"pdkbddl": "<problem text>"}
                         -> epistemic solve via pdkb-planning
  POST /solve-classical  {"domain": "<pddl>", "problem": "<pddl>"}
                         -> full-PDDL classical solve via the bundled BFWS planner

Both POST endpoints return:
  {"ok": bool, "plan": [...], "output": "<raw planner output>",
   "returncode": int, "stats"?: {...}, "error"?: str}

Safety model (important on a small / shared host):
  * The server is SINGLE-THREADED, so solves run one at a time -- a natural queue
    that prevents several planner processes from multiplying memory.
  * Each solve runs in its own process group with an address-space rlimit
    (SOLVE_MEM_MB) and a wall-clock timeout (SOLVE_TIMEOUT). On timeout the whole
    group is killed, so the planner cannot linger.
  * Paired with a hard container memory cap (docker run --memory=...), each solve
    is capped below that, so the solve dies under pressure, not the server or the
    rest of the host.
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

# Bundled satisficing full-PDDL planner (LAPKT BFWS family). Handles features
# pyperplan cannot: negative preconditions, conditional effects, action costs.
CLASSICAL_PLANNER = os.environ.get(
    "CLASSICAL_PLANNER", "/MEP/pdkb-planning/pdkb/planners/siw-then-bfsf"
)


def _limit_memory():
    """preexec_fn: cap the solve subtree's virtual address space."""
    soft = SOLVE_MEM_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (soft, soft))


def run_capped(cmd, cwd):
    """Run a planner command under the memory + time limits.

    Returns (output, returncode) on completion, or raises TimeoutError after the
    process group has been killed.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=_limit_memory,
        start_new_session=True,  # own group, so the whole search tree can be killed
    )
    try:
        out, _ = proc.communicate(timeout=SOLVE_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        raise TimeoutError
    return out.decode("utf-8", "replace"), proc.returncode


def parse_plan(output):
    """Best-effort plan extraction from epistemic planner output.

    `pdkb.planner` prints the plan after a `--{ Plan }--` banner, one numbered
    step per line: `1. (move_c_l1_l2)`. The plan section is scoped when present
    and each action grabbed, tolerating the leading number; otherwise it falls
    back to any bare parenthesised action lines. The full `output` is always
    returned too, so nothing is lost if this misses.
    """
    marker = re.search(r"--\{\s*Plan\s*\}--", output)
    section = output[marker.end():] if marker else output
    actions = re.findall(r"^\s*(?:\d+\.\s*)?\(([^()\n]+)\)\s*$", section, re.MULTILINE)
    return ["(%s)" % a.strip() for a in actions]


def run_planner(pdkbddl_text):
    """Epistemic solve: compile PDKBDDL to classical PDDL and solve (pdkb-planning)."""
    workdir = tempfile.mkdtemp(prefix="solve-")
    path = os.path.join(workdir, "problem.pdkbddl")
    # Always UTF-8: the container locale may be ASCII, and inputs can contain
    # non-ASCII (e.g. an em-dash in a comment).
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(pdkbddl_text)

    try:
        output, rc = run_capped(
            ["python3", "-m", "pdkb.planner", path], cwd=workdir
        )
    except TimeoutError:
        return {
            "ok": False,
            "error": "timeout",
            "output": "Solve exceeded %d s and was stopped." % SOLVE_TIMEOUT,
        }
    finally:
        _cleanup(workdir)

    plan = parse_plan(output)
    return {
        "ok": rc == 0,
        "returncode": rc,
        "plan": plan,
        "output": output[-20000:],  # cap response size
    }


# Stats reported by the BFWS planner on stdout. Staged search (SIW then BFWS) can
# print these more than once; the totals are summed and the cost taken as final.
_STAT_PATTERNS = {
    "cost": r"Plan found with cost:\s*(\d+)",
    "nodesExpanded": r"Nodes expanded during search:\s*(\d+)",
    "nodesGenerated": r"Nodes generated during search:\s*(\d+)",
}


def parse_classical_stats(output):
    stats = {}
    cost = re.findall(_STAT_PATTERNS["cost"], output)
    if cost:
        stats["cost"] = int(cost[-1])
    for key in ("nodesExpanded", "nodesGenerated"):
        nums = re.findall(_STAT_PATTERNS[key], output)
        if nums:
            stats[key] = sum(int(n) for n in nums)
    times = re.findall(r"Total time:\s*([\d.]+)", output)
    if times:
        stats["totalTimeMs"] = round(sum(float(t) for t in times) * 1000, 3)
    return stats


def run_classical(domain_text, problem_text):
    """Full-PDDL classical solve via the bundled BFWS planner."""
    workdir = tempfile.mkdtemp(prefix="classical-")
    domain_path = os.path.join(workdir, "domain.pddl")
    problem_path = os.path.join(workdir, "problem.pddl")
    plan_path = os.path.join(workdir, "plan.txt")
    with open(domain_path, "w", encoding="utf-8") as fh:
        fh.write(domain_text)
    with open(problem_path, "w", encoding="utf-8") as fh:
        fh.write(problem_text)

    try:
        output, rc = run_capped(
            [
                CLASSICAL_PLANNER,
                "--domain", domain_path,
                "--problem", problem_path,
                "--output", plan_path,
            ],
            cwd=workdir,
        )
        plan = _read_plan_file(plan_path)
    except TimeoutError:
        return {
            "ok": False,
            "error": "timeout",
            "output": "Solve exceeded %d s and was stopped." % SOLVE_TIMEOUT,
        }
    finally:
        _cleanup(workdir)

    if not plan:
        return {
            "ok": False,
            "returncode": rc,
            "plan": [],
            "output": output[-20000:],
            "error": "no plan found (the goal may be unreachable or the PDDL uses "
            "an unsupported feature)",
        }
    return {
        "ok": True,
        "returncode": rc,
        "plan": plan,
        "stats": parse_classical_stats(output),
        "output": output[-20000:],
    }


def _read_plan_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip().startswith("(")]


def _cleanup(workdir):
    try:
        for name in os.listdir(workdir):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
        os.rmdir(workdir)
    except OSError:
        pass


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

    def _read_body(self):
        """Return the decoded JSON request body, or None on any problem."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._json(
                413,
                {"ok": False, "error": "missing or too-large body (max %d bytes)" % MAX_BODY},
            )
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid JSON body"})
            return None

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
        route = self.path.rstrip("/")
        if route == "/solve":
            self._handle_solve(self._solve_epistemic)
        elif route == "/solve-classical":
            self._handle_solve(self._solve_classical)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _handle_solve(self, solver):
        data = self._read_body()
        if data is None:
            return
        try:
            result = solver(data)
        except ValueError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except MemoryError:
            result = {"ok": False, "error": "out of memory"}
        except Exception as exc:  # never crash the server on a bad solve
            result = {"ok": False, "error": "solver error: %s" % exc}
        self._json(200, result)

    @staticmethod
    def _solve_epistemic(data):
        text = data.get("pdkbddl")
        if not isinstance(text, str) or not text.strip():
            raise ValueError('expected JSON {"pdkbddl": "<problem text>"}')
        return run_planner(text)

    @staticmethod
    def _solve_classical(data):
        domain = data.get("domain")
        problem = data.get("problem")
        if (
            not isinstance(domain, str) or not domain.strip()
            or not isinstance(problem, str) or not problem.strip()
        ):
            raise ValueError('expected JSON {"domain": "<pddl>", "problem": "<pddl>"}')
        return run_classical(domain, problem)

    def log_message(self, fmt, *args):
        # one terse line per request
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    print(
        "solver API on :%d  (timeout=%ds, mem=%dMB, cors=%s)"
        % (PORT, SOLVE_TIMEOUT, SOLVE_MEM_MB, CORS_ORIGIN)
    )
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
