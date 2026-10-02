# syntax=docker/dockerfile:1
# Hardened image: slim base, non-root user, no build tools at runtime, data only in /data.
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# Optional corporate/proxy CA for the build only (never stored in the image):
#   docker build --secret id=pip_ca,src=/path/ca.pem .
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    python -m venv /opt/venv && /opt/venv/bin/pip install ".[pdf]"

FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CONTADOR_DATA_DIR=/data \
    CONTADOR_DENSE_MODEL=none \
    CONTADOR_ALLOW_FILE_PATHS=0
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /nonexistent app \
    && mkdir -p /data && chown app:app /data && chmod 700 /data
COPY --from=build /opt/venv /opt/venv
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
# Bind inside the container network only; TLS + public exposure is the reverse proxy's job.
ENTRYPOINT ["contador-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
