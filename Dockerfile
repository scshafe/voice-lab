# syntax=docker/dockerfile:1.10
#
# The voice-lab API image (/api/v1; design phase 2, D-031): the standard-library
# server and the package's pydantic closure, nothing else. No model runtime, no
# capture stack, no encryption library. Built by the PC's runner lane from this
# file at the merged commit (deploy/stack/stack.toml) and pinned in the stack's
# .env; locally: `docker build -t voice-lab .`.
#
# The wheel is built from this checkout in the first stage with the exact build
# backend of pyproject.toml (hash-locked); the runtime installs the hash-locked
# pydantic closure (requirements/vault-linux.txt, shared with the vault server)
# and the wheel offline.

ARG PYTHON_IMAGE=python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

FROM ${PYTHON_IMAGE} AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY requirements/development-build.txt requirements/vault-linux.txt /src/requirements/
RUN python -I -m pip install --no-deps --require-hashes -r requirements/development-build.txt \
 && python -I -m pip download --require-hashes --only-binary=:all: --dest /wheels \
      -r requirements/vault-linux.txt
COPY pyproject.toml setup.py MANIFEST.in README.md LICENSE /src/
COPY migrations/ /src/migrations/
COPY src/ /src/src/
RUN python -I -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels /src

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
    PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/nonexistent
COPY --from=build /wheels/ /opt/wheels/
COPY requirements/vault-linux.txt /opt/wheels/requirements.txt
RUN python -I -B -m pip install --no-index --require-hashes --only-binary=:all: \
      --find-links /opt/wheels -r /opt/wheels/requirements.txt \
 && python -I -B -m pip install --no-index --no-deps --no-compile /opt/wheels/cole_voice_lab-*.whl \
 && python -I -B -m pip check \
 && rm -rf /opt/wheels
# The stack runs it as the data root's owner (compose `user:`); never root.
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-I", "-B", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
ENTRYPOINT ["python", "-I", "-B", "-m", "cole_voice_lab.api"]
CMD ["--help"]
