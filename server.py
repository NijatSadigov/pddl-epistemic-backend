#!/usr/bin/env python3
"""Dependency-free HTTP API around pdkb-planning (RP-MEP) and its bundled
classical planner.

Endpoints
  GET  /health           -> {"ok": true}
  POST /solve            {"pdkbddl": "<problem text>"}
                         -> epistemic solve via pdkb-planning
  POST /solve-classical  {"domain": "<pddl>", "problem": "<pddl>", "planner"?: "<id>"}
                         -> full-PDDL classical solve via a selected bundled planner

Both POST endpoints return:
  {"ok": bool, "plan": [...], "output": "<raw planner output>",
   "returncode": int, "stats"?: {...}, "error"?: str}

Safety model (for a small, shared host):
  * The server is single-threaded, so solves run one at a time. This forms a
    queue that prevents several planner processes from multiplying memory.
  * Each solve runs in its own process group with an address-space rlimit
    (SOLVE_MEM_MB) and a wall-clock timeout (SOLVE_TIMEOUT). On timeout the whole
    group is killed.
  * With a hard container memory cap (docker run --memory=...), each solve is
    capped below that, so an oversized solve is killed before the host is.
"""

import collections
import json
import os
import re
import resource
import signal
import subprocess
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "8000"))
SOLVE_TIMEOUT = int(os.environ.get("SOLVE_TIMEOUT", "30"))
SOLVE_MEM_MB = int(os.environ.get("SOLVE_MEM_MB", "600"))
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")
MAX_BODY = 256 * 1024  # reject inputs larger than 256 KB

# Per-IP rate limit for solve requests. This is the public entry point (the
# fast-downward and EFP services are internal-only and reached through here), so
# a simple sliding-window cap protects the host from a flood of solves.
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "20"))
_RATE_HITS = collections.defaultdict(list)


def _client_ip(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


def _rate_limited(ip):
    now = time.time()
    hits = _RATE_HITS[ip]
    cutoff = now - 60
    while hits and hits[0] < cutoff:
        hits.pop(0)
    if not hits and ip in _RATE_HITS:
        # keep the table small: drop idle IPs before re-adding
        del _RATE_HITS[ip]
        hits = _RATE_HITS[ip]
    if len(hits) >= RATE_LIMIT_PER_MIN:
        return True
    hits.append(now)
    return False

# Bundled satisficing full-PDDL planners (LAPKT). They handle features pyperplan
# cannot: negative preconditions, conditional effects, action costs. Requests
# select one by id; the id is validated against this allowlist and the binary
# path is never built from raw input.
PLANNERS_DIR = os.environ.get(
    "PLANNERS_DIR", "/MEP/pdkb-planning/pdkb/planners"
)
CLASSICAL_PLANNERS = {
    "siw-then-bfsf": "siw-then-bfsf",
    "bfws": "bfws",
    "bfs_f": "bfs_f",
    "siw": "siw",
}
DEFAULT_CLASSICAL_PLANNER = "siw-then-bfsf"

# Fast Downward configurations are solved on the separate fast-downward service.
# These client-facing planner ids map to that service's config ids; requests for
# them are proxied over the internal Docker network.
FD_SERVICE_URL = os.environ.get("FD_SERVICE_URL", "http://fast-downward:8000/solve")
FD_PLANNERS = {
    "fd-lama-first": "lama-first",
    "fd-opt-lmcut": "opt-lmcut",
    "fd-opt-blind": "opt-blind",
}

# EFP is a separate native epistemic planner (E-PDDL input). /solve-efp forwards
# to it over the internal Docker network.
EFP_SERVICE_URL = os.environ.get("EFP_SERVICE_URL", "http://efp:8000/solve")

# The bundled pdkb planner reads its input files with the locale default encoding,
# which on this image resolves to ASCII (the C.UTF-8 locale is not effective and
# Python 3.6 has no UTF-8 mode). A single non-ASCII character (for example an
# em-dash or smart quote pasted into a comment) would crash it, so input is
# transliterated to ASCII before use.
_UNICODE_FIXUPS = {
    "—": "-", "–": "-", "‒": "-", "−": "-",   # dashes
    "‘": "'", "’": "'", "“": '"', "”": '"',   # smart quotes
    "…": "...", "→": "->", "·": "-", " ": " ",  # misc
}


def to_ascii(text):
    for uni, repl in _UNICODE_FIXUPS.items():
        text = text.replace(uni, repl)
    return text.encode("ascii", "ignore").decode("ascii")


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
    """Extract the plan from epistemic planner output.

    `pdkb.planner` prints the plan after a `--{ Plan }--` banner, one numbered
    step per line: `1. (move_c_l1_l2)`. The plan section is scoped when present
    and each action read, tolerating the leading number; otherwise this falls
    back to any bare parenthesised action lines. The raw `output` is also
    returned, so the full text is available if parsing does not match.
    """
    marker = re.search(r"--\{\s*Plan\s*\}--", output)
    section = output[marker.end():] if marker else output
    actions = re.findall(r"^\s*(?:\d+\.\s*)?\(([^()\n]+)\)\s*$", section, re.MULTILINE)
    return ["(%s)" % a.strip() for a in actions]


def run_planner(pdkbddl_text):
    """Epistemic solve: compile PDKBDDL to classical PDDL and solve (pdkb-planning)."""
    workdir = tempfile.mkdtemp(prefix="solve-")
    path = os.path.join(workdir, "problem.pdkbddl")
    # The planner reads this file under an ASCII locale, so transliterate any
    # non-ASCII (e.g. an em-dash in a comment) to ASCII before writing.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_ascii(pdkbddl_text))

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


def run_classical(domain_text, problem_text, planner_id):
    """Full-PDDL classical solve via the selected bundled planner.

    `planner_id` must be a validated key of CLASSICAL_PLANNERS.
    """
    planner_path = os.path.join(PLANNERS_DIR, CLASSICAL_PLANNERS[planner_id])
    workdir = tempfile.mkdtemp(prefix="classical-")
    domain_path = os.path.join(workdir, "domain.pddl")
    problem_path = os.path.join(workdir, "problem.pddl")
    plan_path = os.path.join(workdir, "plan.txt")
    with open(domain_path, "w", encoding="utf-8") as fh:
        fh.write(to_ascii(domain_text))
    with open(problem_path, "w", encoding="utf-8") as fh:
        fh.write(to_ascii(problem_text))

    try:
        output, rc = run_capped(
            [
                planner_path,
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
            "planner": planner_id,
            "output": "Solve exceeded %d s and was stopped." % SOLVE_TIMEOUT,
        }
    finally:
        _cleanup(workdir)

    if not plan:
        return {
            "ok": False,
            "returncode": rc,
            "planner": planner_id,
            "plan": [],
            "output": output[-20000:],
            "error": "no plan found (the goal may be unreachable or the PDDL uses "
            "an unsupported feature)",
        }
    return {
        "ok": True,
        "returncode": rc,
        "planner": planner_id,
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


def run_fd_proxy(domain_text, problem_text, planner_id):
    """Forward a Fast Downward solve to the separate fast-downward service."""
    payload = json.dumps({
        "domain": domain_text,
        "problem": problem_text,
        "config": FD_PLANNERS[planner_id],
    }).encode("utf-8")
    req = urllib.request.Request(
        FD_SERVICE_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=SOLVE_TIMEOUT + 25) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "planner": planner_id,
            "error": "fast-downward service unreachable: %s" % exc,
        }
    result["planner"] = planner_id
    return result


def run_efp_proxy(domain_text, problem_text):
    """Forward an E-PDDL solve to the separate EFP service."""
    payload = json.dumps({"domain": domain_text, "problem": problem_text}).encode("utf-8")
    req = urllib.request.Request(
        EFP_SERVICE_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=SOLVE_TIMEOUT + 25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "EFP service unreachable: %s" % exc}


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
        if route in ("/solve", "/solve-classical", "/solve-efp"):
            if _rate_limited(_client_ip(self)):
                return self._json(
                    429,
                    {"ok": False, "error": "rate limit exceeded (max %d solves/min); "
                     "please wait a moment" % RATE_LIMIT_PER_MIN},
                )
        if route == "/solve":
            self._handle_solve(self._solve_epistemic)
        elif route == "/solve-classical":
            self._handle_solve(self._solve_classical)
        elif route == "/solve-efp":
            self._handle_solve(self._solve_efp)
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
        planner_id = data.get("planner") or DEFAULT_CLASSICAL_PLANNER
        if planner_id in CLASSICAL_PLANNERS:
            return run_classical(domain, problem, planner_id)
        if planner_id in FD_PLANNERS:
            return run_fd_proxy(domain, problem, planner_id)
        raise ValueError(
            "unknown planner %r; choose one of: %s"
            % (planner_id, ", ".join(sorted(list(CLASSICAL_PLANNERS) + list(FD_PLANNERS))))
        )

    @staticmethod
    def _solve_efp(data):
        domain = data.get("domain")
        problem = data.get("problem")
        if (
            not isinstance(domain, str) or not domain.strip()
            or not isinstance(problem, str) or not problem.strip()
        ):
            raise ValueError('expected JSON {"domain": "<epddl>", "problem": "<epddl>"}')
        return run_efp_proxy(domain, problem)

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
