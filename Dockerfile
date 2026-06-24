# Epistemic-planning solver backend for PDDL Playground (Phase 2).
#
# Builds Christian Muise's pdkb-planning (RP-MEP): it compiles a multi-agent
# epistemic problem (PDKBDDL) into classical PDDL and solves it with the bundled
# Fast Downward. A tiny dependency-free HTTP API (server.py) exposes POST /solve.
#
# RAM SAFETY — always run with a hard container cap, e.g.
#   docker run --rm -p 8000:8000 --memory=768m --memory-swap=768m pdkb-epistemic
# The server additionally caps each solve's address space (SOLVE_MEM_MB) and
# wall-clock time (SOLVE_TIMEOUT), so a runaway solve dies instead of the host.

FROM ubuntu:18.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Use a UTF-8 locale so the planner can read inputs containing non-ASCII
# characters (e.g. an em-dash in a comment) instead of failing under ASCII.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8

# Build tools (bison/flex/bc for the bundled planner) + python + graphviz stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential bison flex bc expect git ca-certificates \
        python3 python3-dev python3-pip \
        graphviz graphviz-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 18.04 ships Python 3.6. Pin every Python dependency to its last
# 3.6-compatible release so the image still builds today (unpinned installs now
# pull 3.7+-only wheels and break the build).
RUN python3 -m pip install --no-cache-dir --upgrade \
        "pip==21.3.1" "setuptools==59.6.0" "wheel==0.37.1" \
 && python3 -m pip install --no-cache-dir \
        "numpy==1.19.5" "networkx==2.5.1" "pygraphviz==1.6"

# Build pdkb-planning (RP-MEP). --recurse-submodules pulls the example
# epistemic domains (grapevine, corridor, …) used by the self-test.
RUN git clone --recurse-submodules \
        https://github.com/QuMuLab/pdkb-planning.git /MEP/pdkb-planning
WORKDIR /MEP/pdkb-planning
RUN chmod -R 777 pdkb/planners/* && python3 setup.py install

# API layer
COPY server.py /MEP/server.py
WORKDIR /MEP
EXPOSE 8000

# Per-solve limits + CORS (override at runtime). SOLVE_MEM_MB must stay safely
# below the container --memory cap so the solve (not the server) is what dies.
ENV PORT=8000 SOLVE_TIMEOUT=30 SOLVE_MEM_MB=600 CORS_ORIGIN=*
CMD ["python3", "/MEP/server.py"]
