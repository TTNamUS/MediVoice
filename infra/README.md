# Infra

Local development services via Docker Compose.

## Services

| Service | Port | URL |
|---|---|---|
| Qdrant | 6333 | http://localhost:6333/dashboard |
| Postgres | 5432 | `psql postgresql://medivoice:medivoice@localhost:5432/medivoice` |
| Jaeger | 16686 | http://localhost:16686 |
| OTel Collector | 4319 (gRPC) | — |

## Langfuse

**Default**: Langfuse Cloud (free tier). Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` in `apps/server/.env.local`.

**Self-host**: uncomment the `langfuse` and `langfuse-db` services in `docker-compose.yml`. Access at http://localhost:3001.

## OTel Collector endpoint for your app

Your app should set:
```
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319
```
The collector fans out to Jaeger (always) and Langfuse (if env vars set).

## Commands

```bash
make up       # start all services
make down     # stop all services
make logs     # tail logs
make ps       # show status
```
