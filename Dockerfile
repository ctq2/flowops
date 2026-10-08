# FlowOps runs on a stock Python with no third-party packages, so the image is
# deliberately tiny: no compiler, no virtualenv, no pip install step.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# Only what the server actually needs at runtime.
COPY backend/app /app/backend/app
COPY backend/config /app/backend/config
COPY backend/scripts /app/backend/scripts
COPY frontend /app/frontend

# The database lives on a volume so tickets survive a container restart.
RUN mkdir -p /app/data && useradd --create-home --shell /usr/sbin/nologin flowops && chown -R flowops /app
USER flowops

ENV FLOWOPS_DB=/app/data/flowops.db \
    PYTHONPATH=/app/backend

EXPOSE 8787

VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/v1/health', timeout=2).status==200 else 1)"

# 0.0.0.0 so the published port is reachable from outside the container.
CMD ["python", "-m", "app.cli", "serve", "--host", "0.0.0.0", "--port", "8787", "--store", "sqlite", "--seed"]
