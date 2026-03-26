# Local Honcho on Docker Desktop (Windows-friendly reference)

This folder gives you a local Docker Compose setup for Honcho that you can use with Hermes on Windows + Docker Desktop.

It is intentionally based on Honcho's official self-hosted Compose example, but trimmed down to the services you actually need to get a local API running:

- `api`
- `deriver`
- `database` (`pgvector/pgvector:pg15`)
- `redis`

## Why this builds from source

Honcho's official self-hosted setup currently builds from the Honcho source repo rather than pulling a documented prebuilt image. Because of that, this Compose file expects a local Honcho checkout in `docker/honcho/honcho-src` by default.

Official references:

- <https://docs.honcho.dev>
- <https://raw.githubusercontent.com/plastic-labs/honcho/main/docker-compose.yml.example>

## Prerequisites

- Docker Desktop installed and running
- Docker Desktop set to Linux containers
- `git` available in PowerShell

## Quick start

Run these from the Hermes repo root.

### 1. Clone the Honcho server source

```powershell
git clone https://github.com/plastic-labs/honcho.git docker/honcho/honcho-src
```

If you already have a Honcho checkout somewhere else, skip the clone and set `HONCHO_SOURCE_DIR` in `docker/honcho/.env` to that path. On Windows, prefer forward slashes in absolute paths, for example `C:/dev/honcho`.

### 2. Copy the env template

```powershell
Copy-Item docker/honcho/.env.example docker/honcho/.env
```

Optional tweaks in `docker/honcho/.env`:

- `HONCHO_HTTP_PORT` if `8000` is already taken
- `HONCHO_DB_PASSWORD` if you want something other than `postgres`
- `HONCHO_SOURCE_DIR` if you are building from an existing checkout elsewhere

### 3. Build and start Honcho

```powershell
docker compose -f docker/honcho/compose.yaml up -d --build
```

### 4. Verify the containers are healthy

```powershell
docker compose -f docker/honcho/compose.yaml ps
docker compose -f docker/honcho/compose.yaml logs api --tail 100
```

You want to see the services running and the database / redis health checks passing.

## Point Hermes at the local Honcho instance

If Hermes is not already installed with Honcho support, install the extra from this repo:

```powershell
uv pip install -e ".[honcho]"
```

Then point Hermes at the local API:

```powershell
hermes config set HONCHO_BASE_URL http://localhost:8000
hermes honcho status
```

Hermes already supports self-hosted Honcho through `HONCHO_BASE_URL`, so no extra code changes are needed.

## Stop or reset

Stop the stack:

```powershell
docker compose -f docker/honcho/compose.yaml down
```

Stop and delete the database / redis volumes:

```powershell
docker compose -f docker/honcho/compose.yaml down -v
```

## Notes

- This is a local reference stack, not a production deployment recipe.
- The Compose file keeps Postgres and Redis internal to Docker; only the Honcho HTTP port is published to the host.
- If you want to change the host port, edit `HONCHO_HTTP_PORT` in `docker/honcho/.env`.
