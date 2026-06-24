# PDDL Playground — Epistemic Solver Backend (Phase 2)

A small HTTP service that actually **solves multi-agent epistemic planning
problems** for the PDDL Playground. It wraps Christian Muise's
[`pdkb-planning`](https://github.com/QuMuLab/pdkb-planning) (RP-MEP): an
epistemic problem written in **PDKBDDL** is *compiled to classical PDDL* and
solved with the bundled **Fast Downward**.

This runs **separately** from the static Playground site — the Playground stays
fully offline/client-side; this backend is an optional "epistemic mode" it can
call over HTTP.

> ⚠️ **Heads-up:** this was built to run directly on your Linux server (no local
> Ubuntu was available to pre-test it). It wraps research code, so **build and
> run the self-test on the server first** (below) before wiring it to the live
> site. The memory caps mean that even if a solve misbehaves, it dies on its own
> — it can't take your website down.

## What's here

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds `pdkb-planning` + Fast Downward on Ubuntu 18.04, pinned for Python 3.6. |
| `server.py`  | Dependency-free HTTP API: `POST /solve`, `GET /health`. Per-solve memory + time limits, single-threaded queue, CORS. |

## 1. Build

```sh
cd pddl-epistemic-backend
docker build -t pdkb-epistemic .
```

(First build is slow — it compiles the bundled planner. Needs internet.)

## 2. Self-test the planner BEFORE exposing the API

Confirm the planner itself works on a bundled example:

```sh
docker run --rm pdkb-epistemic bash -lc \
  'EX=$(find /MEP/pdkb-planning/examples -name "*.pdkbddl" | head -1); \
   echo "Solving $EX"; python3 -m pdkb.planner "$EX"'
```

You should see the planner run and print a plan. If this works, the hard part is
done.

## 3. Run the API (always with a memory cap)

```sh
docker run -d --name pdkb-epistemic \
  -p 127.0.0.1:8000:8000 \
  --memory=768m --memory-swap=768m \
  -e SOLVE_TIMEOUT=30 -e SOLVE_MEM_MB=600 \
  -e CORS_ORIGIN=https://pddl.yourdomain.com \
  --restart unless-stopped \
  pdkb-epistemic
```

- `--memory=768m` — hard cap for the whole container (protects the host).
- `SOLVE_MEM_MB=600` — per-solve address-space cap (kept **below** the container
  cap, so a heavy solve dies, not the server).
- `SOLVE_TIMEOUT=30` — per-solve wall-clock limit (seconds).
- `CORS_ORIGIN` — your Playground's origin (use the real URL in production; `*`
  only for testing).
- Binding to `127.0.0.1` keeps it private; expose it via your existing reverse
  proxy (see below).

### Test it

```sh
curl http://127.0.0.1:8000/health
# {"ok": true}

curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' \
  --data-binary '{"pdkbddl": "<paste a .pdkbddl problem here>"}'
# {"ok": true, "plan": [...], "output": "...", "returncode": 0}
```

## 4. Put it behind Caddy (your portfolio already uses it)

```caddy
epistemic.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy gives automatic HTTPS. Then point the Playground's epistemic mode at
`https://epistemic.yourdomain.com` (frontend wiring is the next step — not done
yet; see below).

## On the 2 GB box

Safe for small teaching problems (2–3 agents, low knowledge-nesting depth). The
limits above are the safety net: a too-large problem returns
`{"ok": false, "error": "timeout"}` or dies under the memory cap instead of
OOM-ing your site. Keep solves serial (this server already does) and consider
lowering `SOLVE_MEM_MB` if it competes with your main backend.

## API

`POST /solve` — body `{"pdkbddl": "<text>"}` (max 256 KB). Returns:

```json
{ "ok": true, "plan": ["(action …)", "…"], "output": "<raw planner output>", "returncode": 0 }
```

`output` is always the raw planner text — if `plan` parsing ever misses (the
exact print format of `pdkb.planner` may differ), the full output is still there
to read and the parser in `server.py` can be tuned to match.

## Next step (frontend wiring — TODO)

The Playground currently shows the epistemic *explainer* and disables solving.
To connect this backend: add an optional `VITE_EPISTEMIC_API` URL to the
Playground, and in epistemic mode `POST {pdkbddl}` to `/solve` and render the
returned plan (clearly labelled "solved on the server", since it's
network-dependent — the classical playground stays offline).

## Troubleshooting (since it wasn't pre-tested locally)

- **Build fails on a `pip install`** — a pinned version may be unavailable for
  your arch. The pins target Python 3.6 (Ubuntu 18.04); keep the base image at
  `18.04`. As a fallback, the upstream
  [official Dockerfile](https://github.com/QuMuLab/pdkb-planning/blob/master/Dockerfile)
  is known-good; you can build that and add `server.py` on top.
- **`pdkb.planner` import/runtime error** — likely a missing runtime dep
  (`networkx` / `pygraphviz` / `graphviz`); all are installed here, but check the
  self-test output.
- **Self-test finds no `.pdkbddl`** — the examples come from a git submodule;
  the Dockerfile clones with `--recurse-submodules`, but verify with
  `docker run --rm pdkb-epistemic bash -lc 'find /MEP -name "*.pdkbddl" | head'`.
- **Solves always time out / OOM** — raise `SOLVE_TIMEOUT` / `SOLVE_MEM_MB` (and
  the container `--memory`) for a quick check, but keep them bounded in
  production.
