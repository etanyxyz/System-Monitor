# pinned by digest for reproducible builds (python:3.12-slim as of 2026-11-21)
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Unprivileged user; the container runs read-only with all capabilities
# dropped (docker-compose.yml) and needs no root privileges at all.
RUN groupadd --system monitor && useradd --system -g monitor monitor

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=monitor:monitor app ./app
COPY --chown=monitor:monitor static ./static

USER monitor

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
