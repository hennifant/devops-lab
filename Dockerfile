# Pinned to the multi-arch index digest so a given commit always builds on the same
# base layer. Renovate keeps the digest current; do not bump it by hand.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --shell /usr/sbin/nologin appuser

COPY app ./app
# Migrations run from this image too — the compose "migrate" service is the same image
# with a different command. Both paths must be present or `alembic upgrade head` has
# nothing to upgrade to, and /ready cannot look up the expected revision.
COPY alembic.ini .
COPY alembic ./alembic

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

# --no-access-log: the application emits access records as structured fields from a
# middleware instead. --timeout-graceful-shutdown stays below Docker's ten-second grace
# period, so SIGTERM finishes in-flight requests and closes the pool rather than being
# escalated to SIGKILL.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--timeout-graceful-shutdown", "8"]