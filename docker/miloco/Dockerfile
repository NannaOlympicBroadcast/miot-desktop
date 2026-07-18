# Builds the Xiaomi Miloco backend (https://github.com/XiaoMi/xiaomi-miloco)
# into a runnable image for the MIoT Desktop "Miloco" tab.
#
# The desktop app builds this on demand (see backend/miloco_controller.py) when
# the image is missing, then runs it with `--network host`, a bind-mounted
# $MILOCO_HOME, and the Mi OAuth token seeded into its SQLite DB so it comes up
# already bound. The "构建镜像" button's mirror settings (see app.js) map
# straight onto the three ARGs below via --build-arg; the same mirror presets
# are documented in ssr-minimal's own Dockerfile.
#
# Pin a specific tag by passing --build-arg MILOCO_REF=<tag> (default: main).
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG MILOCO_REF=main
ARG PIP_INDEX_URL=https://pypi.org/simple
# Empty = leave apt's default sources (deb.debian.org) alone.
ARG APT_MIRROR=

# Repoint apt at APT_MIRROR when given, then install the runtime libs
# opencv-python-headless / av / onnxruntime need. Debian's slim images ship
# either the classic /etc/apt/sources.list or the newer deb822
# /etc/apt/sources.list.d/debian.sources depending on release — patch
# whichever exists rather than assuming one.
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

# Install the vendored miot library first, then the backend package. Installing
# from the git subdirectories gives hatch-vcs the git metadata it needs; the
# projects declare a fallback-version so an untagged clone still builds.
RUN pip install --no-cache-dir \
      "git+https://github.com/XiaoMi/xiaomi-miloco.git@${MILOCO_REF}#subdirectory=backend/miot" \
 && pip install --no-cache-dir \
      "git+https://github.com/XiaoMi/xiaomi-miloco.git@${MILOCO_REF}#subdirectory=backend/miloco"

# All Miloco state (config.json, miloco.db, models, logs) lives under here; the
# desktop app bind-mounts a host dir onto it.
ENV MILOCO_HOME=/data/miloco
EXPOSE 1810

# miloco.main:start_server — binds 127.0.0.1:1810 by default (reachable from the
# host under --network host).
CMD ["miloco-backend"]
