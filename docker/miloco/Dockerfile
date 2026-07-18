# Builds the Xiaomi Miloco backend (https://github.com/XiaoMi/xiaomi-miloco)
# into a runnable image for the MIoT Desktop "Miloco" tab.
#
# The desktop app builds this on demand (see backend/miloco_controller.py) when
# the image is missing, then runs it with `--network host`, a bind-mounted
# $MILOCO_HOME, and the Mi OAuth token seeded into its SQLite DB so it comes up
# already bound.
#
# Pin a specific tag by passing --build-arg MILOCO_REF=<tag> (default: main).
FROM python:3.11-slim

ARG MILOCO_REF=main

# Runtime libs opencv-python-headless / av / onnxruntime need at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

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
