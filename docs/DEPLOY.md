# Deployment Guide — MediVoice

Three supported deploy targets, in order of preference for a demo or production scenario:

| Target | Best for | Cold-start | gRPC | Notes |
|--------|----------|-----------|------|-------|
| **Pipecat Cloud** | Voice-bot production | None | Via proxy | Managed Pipecat runtime |
| **Fly.io** | General API server | ~1s (disabled) | TCP port 50051 | Good fallback; cheap |
| **Kubernetes** | Production scale | None | Service port 50051 | GKE Autopilot / EKS / Minikube |

---

## Minimum required env vars

Set these in whichever secret store the target uses:

```
ANTHROPIC_API_KEY        # or OPENAI_API_KEY / GEMINI_API_KEY depending on LLM_PROVIDER
DEEPGRAM_API_KEY
CARTESIA_API_KEY
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
LIVEKIT_SIP_TRUNK_ID
DATABASE_URL             # Postgres connection string
QDRANT_URL               # Qdrant instance URL
APP_ENV=production
```

Optional but recommended:
```
LANGFUSE_PUBLIC_KEY      # LLM trace observability
LANGFUSE_SECRET_KEY
OTEL_EXPORTER_OTLP_ENDPOINT   # Jaeger / Grafana Cloud
```

---

## 1. Pipecat Cloud (primary)

Pipecat Cloud manages the Pipecat runtime and worker scaling. LiveKit handles browser WebRTC rooms and SIP.

```bash
# Install Pipecat CLI
pip install pipecat-ai[cli]

# Log in
pcc auth login

# Set secrets (one per line — mirrors .env.local keys)
pcc secrets set ANTHROPIC_API_KEY sk-ant-...
pcc secrets set DEEPGRAM_API_KEY ...
pcc secrets set CARTESIA_API_KEY ...
pcc secrets set LIVEKIT_URL wss://...
pcc secrets set LIVEKIT_API_KEY ...
pcc secrets set LIVEKIT_API_SECRET ...
# Add LIVEKIT_SIP_TRUNK_ID, DATABASE_URL, QDRANT_URL as well

# Deploy (reads pcc-deploy.toml)
cd apps/server
pcc deploy

# Stream logs
pcc logs -f
```

Configuration lives in `apps/server/pcc-deploy.toml`. The `worker_type = "cpu"` line selects CPU-optimized workers; change to `"gpu"` if a local Whisper model replaces Deepgram.

**After deploy**: update your LiveKit SIP trunk dispatch webhook to the Pipecat Cloud URL:
```
https://<your-app>.pipecat.cloud/livekit/dispatch
```

---

## 2. Fly.io (fallback)

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Create app (first time only)
fly launch --no-deploy --name medivoice-server

# Set secrets
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  DEEPGRAM_API_KEY=... \
  CARTESIA_API_KEY=... \
  LIVEKIT_URL=wss://... \
  LIVEKIT_API_KEY=... \
  LIVEKIT_API_SECRET=... \
  LIVEKIT_SIP_TRUNK_ID=... \
  DATABASE_URL=postgresql://... \
  QDRANT_URL=http://...

# Deploy (reads fly.toml at repo root)
fly deploy

# Health check
fly status
curl https://medivoice-server.fly.dev/health
```

`auto_stop_machines = false` in `fly.toml` keeps the machine warm — essential for voice bots that must not cold-start during a live call.

**After deploy**: update LiveKit SIP trunk webhook to `https://medivoice-server.fly.dev/livekit/dispatch`.

---

## 3. Kubernetes (production scale)

See `k8s/README.md` for full instructions. Quick path:

```bash
# Prerequisites: kubectl, cert-manager, ingress-nginx

# 1. Push image (CI does this automatically on push to main)
#    or manually: docker build -t ghcr.io/<user>/medivoice-server:latest apps/server/
#    docker push ghcr.io/<user>/medivoice-server:latest

# 2. Create namespace and secrets
kubectl apply -f k8s/namespace.yaml
kubectl create secret generic medivoice-secrets \
  --from-env-file=apps/server/.env.local \
  --namespace medivoice

# 3. Deploy
kubectl apply -f k8s/

# 4. Watch rollout
kubectl rollout status deployment/medivoice-server -n medivoice

# 5. Verify gRPC health
kubectl port-forward svc/medivoice-server 50051:50051 -n medivoice &
grpcurl -plaintext localhost:50051 medivoice.MediVoice/Check
```

**HPA**: auto-scales from 2→10 pods at 70% CPU. Scale-down has a 5-minute stabilization window to avoid flapping during call bursts.

**Zero-downtime rolling update**: `maxUnavailable: 0` means a new pod must pass the gRPC readiness probe before the old pod is terminated. Never deploy to a cluster with only 1 replica without adjusting this.

---

## Web app (Vercel)

```bash
# From apps/web/
vercel --prod

# Set env in Vercel dashboard:
#   NEXT_PUBLIC_BOT_BASE_URL=https://<your-server-url>
```

Confirm the deployed URL in a browser: open `/clinic`, click mic, speak — the browser connects to your deployed server's `/connect` endpoint.

---

## Smoke test checklist

After any deploy:

- [ ] `GET /health` returns `{"status": "ok"}`
- [ ] `GET /` returns `{"message": "MediVoice API — see /docs"}`
- [ ] Browser session: open `/clinic`, complete a booking turn
- [ ] gRPC health: `grpcurl -plaintext <host>:50051 medivoice.MediVoice/Check` returns `{"status": "SERVING"}`
- [ ] Phone call: dial LiveKit SIP number, ask "What are your hours?" — grounded answer

---

## Rollback

**Pipecat Cloud**: `pcc rollback`

**Fly.io**: `fly releases list` → `fly deploy --image <previous-image>`

**K8s**: `kubectl rollout undo deployment/medivoice-server -n medivoice`
