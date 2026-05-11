# MediVoice — Real-time voice AI for a dental clinic

> Patients book appointments, ask FAQ questions, and get billing answers by speaking — from a browser or a phone.

---

## Architecture

```
┌─────────────────────────────────┐   ┌─────────────────────────────────┐
│   Browser (Next.js + livekit-client) │   Phone (LiveKit SIP)             │
│   LiveKit WebRTC transport      │   │   PSTN → SIP → WebRTC           │
└──────────────┬──────────────────┘   └──────────────┬──────────────────┘
               │  POST /connect                       │  POST /livekit/dispatch
               └──────────────────┬───────────────────┘
                                  ▼
              ┌───────────────────────────────────────────┐
              │        Bot Server  :8000 (FastAPI)        │
              │                   :50051 (gRPC — health,  │
              │                           metrics, eval)  │
              │                                           │
              │  Pipecat Pipeline:                        │
              │    Transport → Deepgram STT (nova-3)      │
              │    → Context aggregator                   │
              │    → Multi-agent orchestrator             │
              │        Triage   FAQ   Booking   Billing   │
              │    → Cartesia TTS (Sonic-2)               │
              │    → MetricsBridge → OTel spans           │
              └──────────────┬────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
   ┌─────────────────────┐    ┌─────────────────────────────────┐
   │  Data layer         │    │  Observability                  │
   │  Qdrant  :6333      │    │  OTel Collector → Jaeger        │
   │  (hybrid RAG)       │    │  Langfuse (LLM traces)          │
   │  Postgres :5432     │    │  Prometheus + Grafana :3002     │
   └─────────────────────┘    └─────────────────────────────────┘
```

Full diagram + design decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Voice pipeline | [Pipecat](https://github.com/pipecat-ai/pipecat) | Native streaming pipeline; one LiveKit transport for browser + SIP |
| LLM | Claude Haiku 4.5 (hot path), Sonnet 4.6 (judge) | Haiku: fast + cheap; Sonnet: eval quality |
| STT | Deepgram nova-3 (streaming) | Lowest TTFB; codec-agnostic |
| TTS | Cartesia Sonic-2 | < 100 ms TTFB SLA |
| WebRTC | LiveKit | One media provider for browser and phone paths |
| Telephony | LiveKit SIP | Full-duplex Opus; no G.711 resampling |
| Vector DB | Qdrant | Hybrid BM25+dense+rerank in one request |
| Embeddings | Voyage-3 dense + BM25 sparse | Domain-general; fine-tuning path documented |
| Reranker | ms-marco-MiniLM-L-6-v2 | 30 ms CPU; LRU-cached per session |
| DB | Postgres + asyncpg | Async; slot locking for booking |
| Frontend | Next.js 15 + livekit-client | Browser mic publishing, bot audio subscription, agent badge |
| API | FastAPI | Async; auto-docs at `/docs` |
| gRPC | grpcio + grpcio-reflection | Health, metrics, eval trigger on :50051 |
| Observability | OTel → Jaeger + Langfuse | Span-level TTFB; LLM token traces |
| Metrics | Prometheus + Grafana | 8-panel dashboard; hallucination gauge |
| SQL validation | sqlglot | AST-level PII firewall before execution |
| Retry | tenacity | Exponential backoff on all external APIs |
| Containers | Docker (multi-stage) | < 400 MB final image |
| Orchestration | Kubernetes (GKE/EKS/Minikube) | HPA 2→10; zero-downtime rolling update |
| CI | GitHub Actions | Lint + test + nightly eval gate + k8s dry-run |

---

## What this project demonstrates


| Tech | Project |
|----------------|---------------|
| **Streaming frameworks: LiveKit** | [`apps/server/api/livekit.py`](apps/server/api/livekit.py) — dispatch, JWT, bot spawn; [`apps/server/bot/transports/livekit_sip.py`](apps/server/bot/transports/livekit_sip.py) |
| **ASR/TTS real-time streaming** | Deepgram nova-3 streaming + Cartesia Sonic-2; TTFB measured in [`docs/LATENCY_BUDGET.md`](docs/LATENCY_BUDGET.md) |
| **RAG pipelines** | Hybrid BM25+dense+rerank in [`apps/server/bot/tools/rag_search.py`](apps/server/bot/tools/rag_search.py); precision@3 eval in `eval/runners/rag_eval.py` |
| **Agentic AI systems** | 4-agent architecture (Triage, FAQ, Booking, Billing) with context-preserving handoff — [`apps/server/bot/agents/`](apps/server/bot/agents/) |
| **Text-to-SQL + NL interfaces** | [`apps/server/bot/tools/sql_query.py`](apps/server/bot/tools/sql_query.py) — NL→SQL with sqlglot AST validation, PII firewall; eval in `eval/datasets/text_to_sql.jsonl` |
| **Performance optimization** | Latency tuning log in [`docs/LATENCY_BUDGET.md`](docs/LATENCY_BUDGET.md); prompt caching, parallel tool calls, sentence-stream TTS; model router in [`apps/server/bot/agents/registry.py`](apps/server/bot/agents/registry.py) |
| **Eval pipelines + hallucination monitoring** | [`eval/runners/`](eval/runners/) — LLM-as-judge (5 dims), drift detector, CI gate in [`.github/workflows/eval.yml`](.github/workflows/eval.yml); results in [`eval/reports/`](eval/reports/) |
| **FastAPI + gRPC** | [`apps/server/main.py`](apps/server/main.py) (FastAPI :8000) + [`apps/server/grpc_server.py`](apps/server/grpc_server.py) (:50051); proto in [`apps/server/proto/medivoice.proto`](apps/server/proto/medivoice.proto) |
| **Kubernetes** | [`k8s/`](k8s/) — Deployment (2 replicas, zero-downtime), HPA (2→10 pods), Ingress + TLS, gRPC readiness probe |
| **Vector DB (Qdrant/Pinecone/Weaviate)** | Qdrant; swap analysis vs Pinecone/Weaviate in [`docs/ARCHITECTURE.md §Vector DB trade-offs`](docs/ARCHITECTURE.md) |
| **Docker** | Multi-stage [`apps/server/Dockerfile`](apps/server/Dockerfile) (~400 MB); [`infra/docker-compose.yml`](infra/docker-compose.yml) for local services |
| **Retry / fallback logic** | [`apps/server/bot/observability/retry.py`](apps/server/bot/observability/retry.py) — `@with_retry` (tenacity), `with_timeout_prompt` (UX on slow tools) |
| **Git / version control** | Conventional commits; CI on push/PR; eval artifacts retained 30 days |

---

## Quick start (local)

### Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- Docker Desktop
- API keys: LLM Provider (OpenAI, Gemini, Anthropic), Deepgram, Cartesia, LiveKit (see `.env.example`)

### 1. Clone and configure

```bash
git clone https://github.com/GITHUB_USER/medivoice
cd medivoice
cp apps/server/.env.example apps/server/.env.local
# Fill in your API keys
```

### 2. Start infra

```bash
make up
# Qdrant    → http://localhost:6333/dashboard
# Jaeger    → http://localhost:16686
# Grafana   → http://localhost:3002  (admin / medivoice)
# Postgres  → postgresql://medivoice:medivoice@localhost:5432/medivoice
```

### 3. Install, seed, and run

```bash
make install       # Python + Node deps
make seed          # Seed Postgres with doctors/patients/slots
make ingest-kb     # Load clinic FAQ into Qdrant
make dev           # Bot server on :8000
make dev-web       # Next.js on :3000 (separate terminal)
```

### 4. Open the demo

Browse to `http://localhost:3000/clinic`, click the mic, and speak.

---

## Running the eval suite

```bash
make eval          # 55 cases, LLM-as-judge, ~$1.50 in API calls
make drift-check   # Compare vs 7-day rolling baseline
make grpc-health   # gRPC Check RPC on :50051
```

Results saved to `eval/reports/run_<timestamp>.json`. CI gate: pass rate ≥ 85%, hallucination rate < 5%.

---

## Deploy

Three supported targets — see [docs/DEPLOY.md](docs/DEPLOY.md) for full instructions.

| Target | Command |
|--------|---------|
| Pipecat Cloud | `cd apps/server && pcc deploy` |
| Fly.io | `fly deploy` |
| Kubernetes | `kubectl apply -f k8s/` |

Docker image built and pushed to `ghcr.io` automatically on push to `main` via [`.github/workflows/docker.yml`](.github/workflows/docker.yml).

---

## Docs

| Doc | Contents |
|-----|----------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Full system diagram, design decisions, transport comparison |
| [LATENCY_BUDGET.md](docs/LATENCY_BUDGET.md) | Stage-by-stage latency targets, tuning log |
| [EVAL_REPORT.md](docs/EVAL_REPORT.md) | Methodology, pass/fail criteria, known failure modes |
| [DEPLOY.md](docs/DEPLOY.md) | Pipecat Cloud / Fly.io / K8s deployment guide |
| [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | 3-minute demo video script with timestamps |
| [PII.md](docs/PII.md) | PII handling, sqlglot firewall, data retention |
| [k8s/README.md](k8s/README.md) | Kubernetes apply guide, scaling, secrets |

---

## Trade-offs and honest caveats

- **Hallucination rate is real and currently failing the CI gate**: 7.3% (4/55) on the eval set — above the 5% medical-domain hard gate. Affected categories: billing (1 case overpromising a callback), edge (1 case claiming Spanish fluency), multi-turn (2 cases: date anchoring and insurance over-specification). FAQ, booking, and SQL categories are 0% hallucination. Fix plan in `docs/EVAL_REPORT.md §Known failure modes`.
- **Latency TBDs**: `LATENCY_BUDGET.md` has TBD fields — these require a real running stack with Jaeger. The methodology is documented; run `make dev` + check `localhost:16686`.
- **LiveKit browser + LiveKit SIP together**: both paths share the same bot code; browser calls `/connect`, phone calls `/livekit/dispatch`.
- **gRPC stubs**: must run `make proto` once after clone before `grpc_server.py` activates. CI does this automatically; local dev requires it manually.
- **`query_appointments_natural` not exposed by default**: the SQL analytics tool is registered as an executable handler but not in any agent's `tool_names` in `registry.py`. Add it to the billing or analytics agent to enable NL→SQL for callers. SQL eval passes at 100% because the eval runner calls it directly.

---

## What's next

1. **Fix hallucination CI gate** *(blocker)*: 4 targeted prompt fixes (billing callback guard, language capability guard, date anchoring, insurance verbatim rule) — expected to bring rate from 7.3% → 0%. See `docs/EVAL_REPORT.md §Known failure modes`.
2. **Hallucination firewall**: post-LLM grounding check (Haiku) before speaking — expected < 0.5% rate at +100 ms latency cost. Addresses root cause structurally.
3. **Fine-tuned embeddings**: voyage-3 → domain-specific dental embeddings. Current RAG precision@3: 0.883; expected after fine-tuning: ~0.93.
4. **SIP cold transfer**: `LivekitParticipant.transfer()` to a human operator for escalations.
5. **Load test**: 20 concurrent sessions → p95 under load (script at `scripts/load_test.py`).
