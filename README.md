# PDDL Playground - Solver Backend

An HTTP service that provides two server-side planners for the PDDL Playground:

- **Epistemic** - multi-agent epistemic planning. Wraps Christian Muise's
  [`pdkb-planning`](https://github.com/QuMuLab/pdkb-planning) (RP-MEP): a problem
  written in **PDKBDDL** is compiled to classical PDDL and solved with the
  bundled **LAPKT BFWS** planner (`siw-then-bfsf`).
- **Classical (full PDDL)** - a satisficing classical solve via the same bundled
  BFWS planner. It accepts PDDL features the in-browser `pyperplan` engine does
  not, including negative preconditions, conditional effects, and action costs.

The service runs separately from the static Playground site. The Playground
itself is fully client-side; this backend is an optional server-side solver that
the frontend calls over HTTP.

The service wraps research code and is intended to run inside its Docker
container. Per-solve memory and time limits are enforced (see [Configuration](#configuration)),
so a misbehaving solve is terminated rather than affecting the host.

## Contents

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds `pdkb-planning` (RP-MEP) and its bundled BFWS planner on Ubuntu 18.04, pinned for Python 3.6. |
| `server.py`  | Dependency-free HTTP API: `POST /solve`, `POST /solve-classical`, `GET /health`. Per-solve memory and time limits, single-threaded queue, CORS. |

## Build

```sh
cd pddl-epistemic-backend
docker build -t pdkb-epistemic .
```

The first build compiles the bundled planner and requires internet access.

## Self-test

Verify the planner runs on a bundled example before exposing the API:

```sh
docker run --rm pdkb-epistemic bash -lc \
  'EX=$(find /MEP/pdkb-planning/examples -name "*.pdkbddl" | head -1); \
   echo "Solving $EX"; python3 -m pdkb.planner "$EX"'
```

The planner should run and print a plan.

## Run

```sh
docker run -d --name pdkb-epistemic \
  -p 127.0.0.1:8000:8000 \
  --memory=768m --memory-swap=768m \
  -e SOLVE_TIMEOUT=30 -e SOLVE_MEM_MB=600 \
  -e CORS_ORIGIN=https://pddl.example.com \
  --restart unless-stopped \
  pdkb-epistemic
```

Binding to `127.0.0.1` keeps the service private; expose it through a reverse
proxy (see [Reverse proxy](#reverse-proxy)).

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `SOLVE_TIMEOUT` | `30` | Per-solve wall-clock limit (seconds). |
| `SOLVE_MEM_MB` | `600` | Per-solve address-space cap (MB). Kept below the container memory cap so a heavy solve is terminated before the container is. |
| `CORS_ORIGIN` | - | Allowed origin for browser requests. Set to the Playground origin in production; `*` for testing only. |
| `CLASSICAL_PLANNER` | `/MEP/pdkb-planning/pdkb/planners/siw-then-bfsf` | Path to the bundled classical planner used by `/solve-classical`. |

The container's `--memory` flag is a hard cap for the whole container and
protects the host. `SOLVE_MEM_MB` is kept below it so an oversized solve is
killed without taking down the service.

### Health and solve

```sh
curl http://127.0.0.1:8000/health
# {"ok": true}

# Epistemic (PDKBDDL)
curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' \
  --data-binary '{"pdkbddl": "<.pdkbddl problem>"}'
# {"ok": true, "plan": [...], "output": "...", "returncode": 0}

# Classical (full PDDL)
curl -s -X POST http://127.0.0.1:8000/solve-classical \
  -H 'Content-Type: application/json' \
  --data-binary '{"domain": "<domain pddl>", "problem": "<problem pddl>"}'
# {"ok": true, "plan": [...], "stats": {...}, "output": "...", "returncode": 0}
```

## Reverse proxy

Example Caddy configuration:

```caddy
epistemic.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy provisions HTTPS automatically. The Playground's epistemic mode is then
built with `VITE_EPISTEMIC_API=https://epistemic.example.com`.

## Resource sizing

The default limits suit small teaching problems (2-3 agents, low
knowledge-nesting depth). An oversized problem returns
`{"ok": false, "error": "timeout"}` or is terminated under the memory cap rather
than exhausting host memory. Solves are processed serially. On a memory-
constrained host, lower `SOLVE_MEM_MB` if the service competes with other
workloads.

## API

All request bodies are limited to 256 KB.

### `POST /solve` - epistemic

Body `{"pdkbddl": "<text>"}`. Compiles PDKBDDL to classical PDDL and solves it.

```json
{ "ok": true, "plan": ["(action …)", "…"], "output": "<raw planner output>", "returncode": 0 }
```

### `POST /solve-classical` - full PDDL

Body `{"domain": "<pddl>", "problem": "<pddl>"}`. Solves with the bundled BFWS
planner.

```json
{
  "ok": true,
  "plan": ["(ACTION ARGS)", "…"],
  "stats": { "cost": 7, "nodesExpanded": 17, "nodesGenerated": 27, "totalTimeMs": 0.7 },
  "output": "<raw planner output>",
  "returncode": 0
}
```

When no plan is found, the response is `{"ok": false, "error": "no plan found …",
"output": "…"}`.

For both endpoints, `output` always contains the raw planner text. If plan
parsing does not match a given planner print format, the full output remains
available and the parser in `server.py` can be adjusted.

### `GET /health`

Returns `{"ok": true}`.

## Troubleshooting

- **Build fails on a `pip install`** - a pinned version may be unavailable for
  the target architecture. The pins target Python 3.6 (Ubuntu 18.04); keep the
  base image at `18.04`. The upstream
  [official Dockerfile](https://github.com/QuMuLab/pdkb-planning/blob/master/Dockerfile)
  is a known-good fallback that `server.py` can be layered on top of.
- **`pdkb.planner` import or runtime error** - typically a missing runtime
  dependency (`networkx` / `pygraphviz` / `graphviz`). All are installed in this
  image; check the self-test output.
- **Self-test finds no `.pdkbddl`** - the examples come from a git submodule.
  The Dockerfile clones with `--recurse-submodules`; verify with
  `docker run --rm pdkb-epistemic bash -lc 'find /MEP -name "*.pdkbddl" | head'`.
- **Solves always time out or OOM** - raise `SOLVE_TIMEOUT` / `SOLVE_MEM_MB`
  (and the container `--memory`) for diagnosis, keeping them bounded in
  production.

## Acknowledgement

Developed with assistance from Claude Code, used to refine the design and the
wording of the documentation and UI.
