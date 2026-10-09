# syntax=docker/dockerfile:1.10
#
# Voice Journey server image: corpus browser + watcher + intake API, with the
# analysis toolchain (ffmpeg, whisper.cpp CPU, praat/librosa venv).
#
# Build (the private @scshafe/ui install needs GitHub Packages read; the token
# is a BuildKit SECRET, never a build arg, env or layer):
#
#   docker build --secret id=node_auth_token,env=NODE_AUTH_TOKEN -t voice-journey .
#   docker build --target test --secret id=node_auth_token,env=NODE_AUTH_TOKEN .   # runs npm test in the image
#
# Run: see docs/move-to-lubuntu.md (infra stack `voice-journey`). State lives
# in the /data volume; the image itself is read-only-safe.

ARG NODE_IMAGE=node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6

# ---- whisper.cpp (CPU), pinned to v1.9.4 ------------------------------------
FROM ${NODE_IMAGE} AS whisper
ARG WHISPER_CPP_COMMIT=927cfce34f31707e17f2bff35c349632fb9e2c3a
# Portable x86-64 build (GGML_NATIVE off) with the AVX2 baseline; override for
# older CPUs or ARM hosts.
ARG WHISPER_CMAKE_FLAGS="-DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_BMI2=ON"
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q . \
 && git fetch -q --depth 1 https://github.com/ggml-org/whisper.cpp "${WHISPER_CPP_COMMIT}" \
 && git checkout -q FETCH_HEAD
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF ${WHISPER_CMAKE_FLAGS} \
        -DWHISPER_BUILD_TESTS=OFF \
 && cmake --build build --config Release -j"$(nproc)" --target whisper-cli \
 && strip build/bin/whisper-cli

# ---- Python venv (praat-parselmouth, librosa), pinned by analysis/requirements.txt
FROM ${NODE_IMAGE} AS venv
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
COPY analysis/requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# ---- web SPA, built at image time ------------------------------------------
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json web/.npmrc ./
# NODE_AUTH_TOKEN must be a GitHub Packages read token: an agent login shell
# exports it from the broker (npm-token); a deploy gets the deploy job's own
# token (deploy/stack/stack.toml build_secrets; packages: read, which needs
# @scshafe/ui's Actions access grant for this repository). A GitHub App token
# (gh-token) cannot read packages.
# web/.npmrc maps @scshafe to npm.pkg.github.com and reads ${NODE_AUTH_TOKEN}.
# The secret exists only for this RUN: it is not in any layer, env or history.
RUN --mount=type=secret,id=node_auth_token \
    NODE_AUTH_TOKEN="$(cat /run/secrets/node_auth_token)" npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- runtime ----------------------------------------------------------------
FROM ${NODE_IMAGE} AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg python3 libgomp1 libsndfile1 curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Non-root. The in-image user (10001) is the default for standalone runs.
# Do NOT build with APP_UID=1000: the base image's `node` user already owns
# uid 1000, so useradd fails. The infra stack instead runs this image with
# compose `user: "<uid>:<gid>"` matching the data volume's owner (Lubuntu:
# 1000:1000); nothing here needs that uid to exist in /etc/passwd or to have a
# home (see the HOME/cache variables below). APP_UID/APP_GID remain for a uid
# other than 1000.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" voicejourney \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin voicejourney \
 && mkdir -p /data && chown voicejourney:voicejourney /data

COPY --from=whisper /src/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=venv /opt/venv /opt/venv

# Source tree: root-owned and read-only to the app. The only input manifest
# (manifests/chains.json) ships here; generated manifests live in /data.
WORKDIR /app
COPY package.json ./
COPY src ./src
COPY analysis/requirements.txt analysis/extract_features.py analysis/selftest.py ./analysis/
COPY manifests/chains.json ./manifests/chains.json
COPY --from=web /web/dist ./web/dist
COPY scripts/fetch-whisper-model.sh /usr/local/bin/vj-fetch-model

ENV NODE_ENV=production \
    VOICE_JOURNEY_CONTAINER=1 \
    VOICE_JOURNEY_DATA=/data \
    VOICE_JOURNEY_HOST=0.0.0.0 \
    VOICE_JOURNEY_PORT=8787 \
    FFMPEG_BIN=/usr/bin/ffmpeg \
    WHISPER_CPP_BIN=/usr/local/bin/whisper-cli \
    WHISPER_CPP_MODEL=/data/models/ggml-medium.bin \
    VOICE_JOURNEY_PYTHON=/opt/venv/bin/python \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba-cache \
    MPLCONFIGDIR=/tmp/matplotlib \
    JOBLIB_TEMP_FOLDER=/tmp \
    PYTHONDONTWRITEBYTECODE=1

# No account has a home directory (--no-create-home) and the stack may run
# under any uid, so HOME and every cache the Python/Node tools write point at
# /tmp (compose should mount a tmpfs there when the root fs is read-only).

VOLUME /data
EXPOSE 8787
USER voicejourney

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.VOICE_JOURNEY_PORT||8787)+'/healthz').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"

ENTRYPOINT ["/usr/bin/tini", "--"]
# Set VOICE_JOURNEY_WATCH=1 plus VOICE_JOURNEY_WATCH_APPROVAL and
# VOICE_JOURNEY_PLAYBACK_APPROVAL (compose env) to enable the watcher / audio.
CMD ["node", "src/corpus-browser.mjs"]

# ---- test target: `docker build --target test` runs npm test in the image ----
# Not the default target. Runs as the unprivileged app user (root would defeat
# the permission-denied tests) with no data root, so the tests use the working
# tree's local-artifacts/ scratch space exactly as on a developer machine; the
# tree is chowned for this stage only. Nothing from this stage ships.
FROM runtime AS test
USER root
ENV VOICE_JOURNEY_DATA= VOICE_JOURNEY_CONTAINER= VOICE_JOURNEY_HOST=
COPY test ./test
RUN chown -R voicejourney:voicejourney /app
USER voicejourney
RUN npm test

# Make the shipped image the default target.
FROM runtime
