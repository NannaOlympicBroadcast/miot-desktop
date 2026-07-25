# syntax=docker/dockerfile:1
# Builds the Xiaomi Miloco backend (https://github.com/XiaoMi/xiaomi-miloco)
# into a runnable image for the MIoT Desktop "Miloco" tab.
#
# The desktop app builds this on demand (see backend/miloco_controller.py) when
# the image is missing, then runs it with `--network host`, a bind-mounted
# $MILOCO_HOME, and the Mi OAuth token seeded into its SQLite DB so it comes up
# already bound. The "构建镜像" button's mirror settings (see app.js) map
# straight onto the ARGs below via --build-arg; the same mirror presets are
# documented in ssr-minimal's own Dockerfile.
#
# Pin a specific tag/branch/commit by passing --build-arg MILOCO_REF=<ref>
# (default: main).
#
# Three stages:
#   1. `source`      — one shallow-ish fetch of the upstream repo (git history
#                      isn't needed beyond hatch-vcs's version lookup).
#   2. `web-builder`  — builds the React/Vite dashboard (web/) and drops the
#                      output into backend/miloco/src/miloco/static, exactly
#                      where miloco.main:spa_handler expects it (see
#                      miloco.config.settings.DirectorySettings.static_dir).
#                      Skipping this step is what used to make GET / 404: pip
#                      installing backend/miloco directly never runs the web
#                      build, so static_dir/index.html never existed and the
#                      backend's own SPA handler 404s by design when it's
#                      missing — the image had a working API and no dashboard.
#   3. final stage    — python runtime; installs the two backend packages via
#                      a build-time-only bind mount of `web-builder`'s source
#                      tree, so Node/pnpm/node_modules never enter this image.
ARG BASE_IMAGE=python:3.11-slim
ARG NODE_IMAGE=node:22-slim

# ---------------------------------------------------------------------------
# Stage 1: fetch the upstream repo once, shared by the web and python builds.
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS source
ARG MILOCO_REF=main
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# `git fetch <arbitrary ref> --depth 1` (rather than `clone --branch`) accepts
# branch names, tags, *and* full commit SHAs — matching what `pip install
# git+URL@ref` used to support. Depth 1 means no tag history reaches
# hatch-vcs, so the build falls back to the `fallback_version = "0.0.0"` both
# backend packages declare — an already-accepted degraded case, not new.
RUN git init -q . \
    && git remote add origin https://github.com/XiaoMi/xiaomi-miloco.git \
    && git fetch --depth 1 origin "${MILOCO_REF}" \
    && git checkout -q FETCH_HEAD

# ---------------------------------------------------------------------------
# Stage 2: build the web dashboard.
# ---------------------------------------------------------------------------
FROM source AS web-builder
# Optional registry mirror (e.g. https://registry.npmmirror.com) for
# restricted/CN networks — same idea as PIP_INDEX_URL/APT_MIRROR below.
# COREPACK_NPM_REGISTRY has to be set *before* `corepack prepare` — that
# command itself fetches the pnpm tarball from npmjs.org, so on a fully
# blocked network it fails before `pnpm config set registry` below ever runs.
ARG NPM_REGISTRY=
ENV COREPACK_NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmjs.org}
RUN corepack enable && corepack prepare pnpm@11 --activate
WORKDIR /src/web
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    if [ -n "$NPM_REGISTRY" ]; then pnpm config set registry "$NPM_REGISTRY"; fi \
    && CI=true pnpm install --frozen-lockfile \
    && pnpm build

# Place the build output where the backend serves it from. Mirrors
# xiaomi-miloco's own scripts/build.sh::build_web (item list, sourcemap
# stripping, and the "all of these must exist" check are copied from there).
RUN set -eu; \
    web_static=/src/backend/miloco/src/miloco/static; \
    rm -rf "$web_static"; mkdir -p "$web_static"; \
    for item in index.html assets fonts favicon.svg watch.html vendor; do \
        [ -e "dist/$item" ] && cp -R "dist/$item" "$web_static/"; \
    done; \
    rm -f "$web_static"/assets/*.map; \
    test -f "$web_static/index.html"; \
    test -d "$web_static/assets"; \
    test -d "$web_static/fonts"; \
    test -f "$web_static/watch.html"

# ---------------------------------------------------------------------------
# Stage 3: runtime image — python only, no Node/pnpm/node_modules.
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
# Empty = leave apt's default sources (deb.debian.org) alone.
ARG APT_MIRROR=

# Repoint apt at APT_MIRROR when given, then install the runtime libs
# opencv-python-headless / av / onnxruntime need, plus git — still needed at
# *build* time below for hatch-vcs to read the bind-mounted repo's history.
# Debian's slim images ship either the classic /etc/apt/sources.list or the
# newer deb822 /etc/apt/sources.list.d/debian.sources depending on release —
# patch whichever exists rather than assuming one.
RUN if [ -n "$APT_MIRROR" ]; then \
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        [ -f "$f" ] && sed -i \
          -e "s|deb.debian.org|${APT_MIRROR}|g" \
          -e "s|security.debian.org|${APT_MIRROR}|g" \
          "$f"; \
      done; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1

# Install the vendored miot library first, then the backend package, from a
# *build-time-only* bind mount of web-builder's full source tree (already
# containing the built static/ from stage 2 and the .git history hatch-vcs's
# `root = "../.."` walks up to). A bind mount — as opposed to COPY — never
# lands in an image layer, so this is how Node/pnpm/node_modules from earlier
# stages stay out of the final image entirely.
RUN --mount=type=bind,from=web-builder,source=/src,target=/src \
    pip install --no-cache-dir /src/backend/miot \
    && pip install --no-cache-dir /src/backend/miloco

# All Miloco state (config.json, miloco.db, models, logs) lives under here; the
# desktop app bind-mounts a host dir onto it.
ENV MILOCO_HOME=/data/miloco
EXPOSE 1810

# miloco.main:start_server — binds 127.0.0.1:1810 by default (reachable from the
# host under --network host).
CMD ["miloco-backend"]
