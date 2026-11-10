# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml .
COPY app ./app
# Optional extra CA certificate for builds behind a TLS-intercepting proxy:
#   docker build --secret id=extra_ca,src=/path/to/ca.pem ...
# Without the secret this is a plain pip install.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir .

COPY alembic.ini .
COPY migrations ./migrations
COPY bin/run-collector /app/bin/run-collector
RUN chmod 755 /app/bin/run-collector \
    && useradd --create-home --uid 10001 dexbot

# Long-running launch collector. No HTTP port: a cron cannot hit the 1-minute slot.
FROM base AS collector
USER dexbot
CMD ["/app/bin/run-collector"]

# Default image remains the paper API. `docker build` without --target uses this stage.
FROM base AS api
USER dexbot
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
