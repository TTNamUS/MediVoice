.PHONY: up down logs ps dev dev-web install lint format test check-env ingest-kb seed eval eval-rag eval-rag-e2e eval-booking eval-sql eval-sql-e2e eval-handoff eval-phone drift-check proto grpc-health tunnel help

INFRA_DIR  := infra
SERVER_DIR := apps/server
WEB_DIR    := apps/web

# ── Infra ─────────────────────────────────────────────────────────────────────

up:
	docker compose -f $(INFRA_DIR)/docker-compose.yml up -d
	@echo ""
	@echo "  Qdrant      → http://localhost:6333/dashboard"
	@echo "  Jaeger      → http://localhost:16686"
	@echo "  Grafana     → http://localhost:3002  (admin / medivoice)"
	@echo "  Prometheus  → http://localhost:9090"
	@echo "  Postgres    → psql postgresql://medivoice:medivoice@localhost:5432/medivoice"
	@echo ""

down:
	docker compose -f $(INFRA_DIR)/docker-compose.yml down

logs:
	docker compose -f $(INFRA_DIR)/docker-compose.yml logs -f

ps:
	docker compose -f $(INFRA_DIR)/docker-compose.yml ps

# ── Dev servers ───────────────────────────────────────────────────────────────

dev:
	@echo "Starting server on http://localhost:8000"
	cd $(SERVER_DIR) && uv run --no-sync uvicorn main:app --reload --host 0.0.0.0 --port 8000

dev-web:
	@echo "Starting Next.js on http://localhost:3000"
	cd $(WEB_DIR) && npm run dev

# ── Dependencies ──────────────────────────────────────────────────────────────

install:
	cd $(SERVER_DIR) && uv venv && uv pip install -e ".[dev]"
	@if [ -f $(WEB_DIR)/package.json ]; then cd $(WEB_DIR) && npm install; fi

# ── Code quality ─────────────────────────────────────────────────────────────

lint:
	cd $(SERVER_DIR) && uv run --no-sync ruff check .

format:
	cd $(SERVER_DIR) && uv run --no-sync ruff format .

# ── Tests ─────────────────────────────────────────────────────────────────────

test:
	cd $(SERVER_DIR) && uv run --no-sync pytest -v

# ── Environment check ─────────────────────────────────────────────────────────

check-env:
	uv run python scripts/check_env.py

# ── Data / eval ────────────────────────────────────────────────────────────────


ingest-kb:
	cd $(SERVER_DIR) && uv run --no-sync python -m ingest.load_clinic_kb

seed:
	cd $(SERVER_DIR) && uv run --no-sync python ../../scripts/seed_db.py

eval:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/offline_eval.py

eval-rag:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/rag_eval.py

eval-rag-e2e:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/rag_eval.py --e2e

eval-booking:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/booking_eval.py

eval-sql:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/sql_eval.py

eval-sql-e2e:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/sql_eval.py --execute

eval-handoff:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/handoff_eval.py

eval-phone:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/phone_eval.py

drift-check:
	cd $(SERVER_DIR) && uv run --no-sync python ../../eval/runners/drift_detector.py

# ── gRPC ────────────────────────────────────────────────────────────────────────


proto:
	@echo "Compiling protobuf stubs..."
	mkdir -p $(SERVER_DIR)/proto/generated
	cd $(SERVER_DIR) && uv run --no-sync python -m grpc_tools.protoc \
		-I proto \
		--python_out=proto/generated \
		--grpc_python_out=proto/generated \
		proto/medivoice.proto
	@echo "Stubs written to $(SERVER_DIR)/proto/generated/"

grpc-health:
	grpcurl -plaintext localhost:50051 medivoice.MediVoice/Check

# ── Tunnel (LiveKit dispatch webhook) ──────────────────────────────────────────


tunnel:
	@echo "Starting ngrok tunnel → http://localhost:8000"
	ngrok http 8000

# # ── Help ──────────────────────────────────────────────────────────────────────

# help:
# 	@echo ""
# 	@echo "  make up          Start local infra (Qdrant, Postgres, Jaeger)"
# 	@echo "  make down        Stop local infra"
# 	@echo "  make dev         Start bot server (port 8000)"
# 	@echo "  make dev-web     Start Next.js (port 3000)"
# 	@echo "  make install     Install Python + Node deps"
# 	@echo "  make lint        Run ruff lint"
# 	@echo "  make format      Run ruff format"
# 	@echo "  make test        Run pytest"
# 	@echo "  make check-env   Validate API keys"
# 	@echo "  make ingest-kb      Ingest clinic KB into Qdrant"
# 	@echo "  make seed           Seed Postgres with doctors/patients"
# 	@echo "  make eval           Run full offline eval suite"
# 	@echo "  make eval-rag       RAG retrieval eval: hybrid vs dense-only P@3/MRR"
# 	@echo "  make eval-rag-e2e   RAG E2E text eval: search + fact-check"
# 	@echo "  make eval-booking   Booking scenario structural eval"
# 	@echo "  make eval-sql       Text-to-SQL eval (schema validation)"
# 	@echo "  make eval-sql-e2e   Text-to-SQL eval (execute against DB)"
# 	@echo "  make eval-handoff   Multi-agent handoff routing eval"
# 	@echo "  make eval-phone       Phone scenario eval (automated checks + manual SIP checklist)
#   make drift-check      Model drift detection vs 7-day rolling baseline
#   make proto            Compile protobuf stubs into proto/generated/
#   make grpc-health      grpcurl health check against localhost:50051
#   make tunnel           Start ngrok tunnel for LiveKit dispatch webhook"
# 	@echo ""
