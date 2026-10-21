# Only build from scripts/stage-vault-image's private, offline context.
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
    PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/nonexistent
COPY wheels/ /opt/vault-wheels/
RUN cd /opt/vault-wheels && sha256sum --check wheel.sha256 \
    && python -I -B -m pip install --no-index --require-hashes --only-binary=:all: --find-links /opt/vault-wheels -r vault-linux.txt \
    && python -I -B -m pip install --no-index --no-deps --no-compile cole_voice_lab-*.whl \
    && python -I -B -m pip check \
    && rm -rf /opt/vault-wheels \
    && groupadd --gid 10001 vault \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin vault \
    && mkdir -p /var/lib/voice-lab-vault/data /var/lib/voice-lab-vault/logs \
    && chown -R 10001:10001 /var/lib/voice-lab-vault \
    && chmod 0700 /var/lib/voice-lab-vault /var/lib/voice-lab-vault/data /var/lib/voice-lab-vault/logs
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-I", "-B", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
ENTRYPOINT ["python", "-I", "-B", "-m", "cole_voice_lab.storage_server"]
CMD ["serve", "--config", "/run/secrets/vault_config", "--data-root", "/var/lib/voice-lab-vault/data", "--access-log", "/var/lib/voice-lab-vault/logs/access.jsonl", "--listen-internal", "--port", "8080"]
