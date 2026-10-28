# Multi-stage build: compile the web client, then a slim Python 3.12 runtime (LLD 2).
#
# Stage order matters here. A build with no --target produces the LAST stage in the file, so
# `runtime` must stay at the bottom: with the test stage last, `docker compose up --build` built
# the test image and ran it as the API, which is both wrong and slow. docker-compose.yml names
# `target: runtime` explicitly as well, so neither guard depends on the other.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- Test image -------------------------------------------------------------------------------
# The runtime image below deliberately carries no tests and no test tooling — shipping a test
# framework into the thing that serves real applicant data would be wrong. This stage adds the
# development dependencies and a browser, and takes no copy of the source: the `tests` service in
# docker-compose bind-mounts the working tree instead, so editing a test needs no rebuild.
# Built only on demand, by `docker compose --profile test`.
FROM python:3.12-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
WORKDIR /srv
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt \
 && playwright install --with-deps chromium

# --- Runtime image ----------------------------------------------------------------------------
# Last on purpose: see the note at the top of this file.
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
RUN useradd --create-home --uid 10001 ese
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=web /web/dist ./web/dist
# The uploads volume is mounted here. Create it in the image and give it to the runtime user, so a
# named volume inherits that ownership: a root-owned volume would fail the first document upload.
RUN mkdir -p /srv/var/storage /srv/var/log && chown -R ese:ese /srv/var
USER ese
EXPOSE 8000
# Behind the load balancer, forward the client address so the audit trail records it (LLD 3.10).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
