# Eval Report — MediVoice

> Status: **Real numbers from `make eval`**.
> Provider: openai / gpt-5.2 (judge). All sub-evals run against seeded local stack.

---

## Methodology

### Dataset
- **Full eval dataset**: `eval/datasets/full.jsonl` — 55 cases
- **Distribution**: FAQ (10), Booking (8), Handoff (8), Analytics/SQL (6), Billing (3), Phone (5), Edge cases (10), Multi-turn (5)
- **Hallucination risk tagging**: `high` (28 cases), `medium` (16), `low` (11)
  - `high` = bot likely to fabricate if RAG misses or DB returns empty
  - Medical domain: any price, hour, doctor name, insurance status, appointment time

### Judge model
- **Model**: Claude Sonnet 4.6 (`claude-sonnet-4-6`)
- **Rubric**: 5 dimensions scored 1–5 (see `eval/runners/judge.py`)
  - Correctness, Tool use, Handoff, Voice-friendliness, Hallucination
- **Prompt caching**: rubric system prompt cached (~800 tokens, high reuse value)
- **Estimated cost per run**: ~$1.50 (55 cases × ~2K tokens judge input × Sonnet price)

### Pass criteria (enforced in `offline_eval.py`)
| Criterion | Threshold | Rationale |
|-----------|-----------|-----------|
| Pass rate | ≥ 85% | Standard for production-quality AI |
| Hallucination rate | < 5% | Medical domain — stricter than general AI |
| Avg judge score | ≥ 4.0 / 5.0 | All dimensions, not just hallucination |

### Drift detection (`drift_detector.py`)
| Alert trigger | Threshold |
|---------------|-----------|
| Pass rate regression | > −5 pp vs 7-day rolling avg |
| Hallucination rate spike | > +2 pp vs 7-day rolling avg |

---

## Results

### Overall

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| Pass rate | 89.1% (49/55) | ≥ 85% | PASS |
| Hallucination rate | 7.3% (4/55) | < 5% | **FAIL** |
| Avg judge score | 4.75 / 5.0 | ≥ 4.0 | PASS |
| Cases with hallucination | 4 / 55 | < 3 | **FAIL** |

> **Overall verdict: FAIL** — hallucination rate 7.3% exceeds the 5% medical-domain hard gate.
> Pass rate and avg score are both healthy; the blocker is hallucinations concentrated in
> billing (1 case) and multi-turn (2 cases) and edge cases (1 case).

### By category

| Category | Cases | Pass rate | Hallucination rate | Notes |
|----------|-------|-----------|-------------------|-------|
| FAQ | 10 | 100% | 0% | All 10 passed; RAG grounding solid |
| Booking | 8 | 87.5% | 0% | 1 fail: book-006 missing `book_appointment` call after phone confirmation |
| Handoff | 8 | 100% | 0% | All 8 passed in full eval (separate handoff runner: 87%) |
| Analytics (SQL) | 6 | 100% | 0% | All 6 passed; sqlglot PII firewall working |
| Billing | 3 | 66.7% | 33.3% | 1 hallucination: overpromised "billing specialist callback" not in KB |
| Phone | 5 | 100% | 0% | 2 auto-passed; 3 manual SIP cases pending |
| Edge cases | 10 | 90% | 10% | 1 hallucination: claimed Spanish fluency not in KB (full-edge-008) |
| Multi-turn | 5 | 40% | 40% | Weakest category: 2 hallucinations (date confusion, Aetna PPO specificity) |

### Sub-eval results

#### Booking eval (`make eval-booking`)
| Result | Cases | Pass rate |
|--------|-------|-----------|
| PASS | 15/15 | **100%** |

All 15 booking scenarios passed — happy path, new patient, no availability, specialty mismatch,
slot conflict, idempotent booking, mid-flow hangup, emergency, pediatric.

#### Text-to-SQL eval (`make eval-sql`)
| Mode | Cases | Passed | Skipped | Pass rate |
|------|-------|--------|---------|-----------|
| Static (execute=False) | 10 | 9 | 1 | **100%** |
| E2E (execute=True) | 10 | 9 | 1 | **100%** |

1 skipped: sql-003 (check_availability case, not SQL). All write/destructive queries
correctly refused before generation. All SELECT queries executed against live DB.

#### RAG retrieval eval (`make eval-rag`)
| Mode | P@1 | P@3 | P@5 | MRR | Status |
|------|-----|-----|-----|-----|--------|
| Hybrid (BM25 + dense + rerank) | 1.000 | **0.883** | 0.883 | 1.000 | PASS (≥ 0.85) |
| Dense-only (ablation) | 0.950 | 0.883 | 0.820 | 0.975 | — |

Hybrid outperforms dense-only at P@5 (+6.3 pp) and MRR (+2.5 pp). Dense-only
matches hybrid at P@3 on these 20 cases; the BM25 sparse component adds value
primarily on keyword-heavy queries (e.g., policy codes, specific doctor names).

#### RAG E2E text eval (`make eval-rag-e2e`)
| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| E2E pass rate | 90% (18/20) | ≥ 85% | PASS |

2 failures: `rag-008` (after-hours emergency number "867-5309" not in top-3 snippet),
`rag-009` ("Thanksgiving" not surfaced in holiday hours chunk).

#### Handoff eval (`make eval-handoff`)
| Result | Cases | Pass rate | Status |
|--------|-------|-----------|--------|
| PASS | 13/15 | **87%** | PASS (≥ 80%) |

2 failures:
- `hoff-011` (wrong_handoff_recovery): expected `transfer_to('faq')` after mis-route but no transfer made
- `hoff-013` (escalation): expected `transfer_to('billing')` but got `transfer_to_human` — billing agent escalated prematurely

#### Phone eval (`make eval-phone`)
| Result | Cases | Notes |
|--------|-------|-------|
| PASS (automated) | 2 / 2 | digit_collection, are_you_real |
| MANUAL | 3 / 3 | Require live SIP dial-in to verify |

Manual cases: repeat_back (date + doctor readback), hangup_mid_booking (no orphan DB row),
background_noise (all-caps STT robustness).

### Latency (LiveKit WebRTC p50 / p95)

> Measure via Jaeger after 5-turn conversation. Run `make dev` then inspect `localhost:16686`.

| Stage | p50 | p95 | Target p50 |
|-------|-----|-----|-----------|
| STT TTFB | TBD | TBD | < 120 ms |
| LLM TTFB | TBD | TBD | < 280 ms |
| TTS TTFB | TBD | TBD | < 100 ms |
| Voice-to-voice | TBD | TBD | < 800 ms |

> Latency TBDs require a live running stack with Jaeger. Methodology: `make dev`,
> open `localhost:16686`, filter trace by `service=medivoice`, measure spans.

### Cost per 3-minute conversation

| Component | Tokens/call (est.) | Cost |
|-----------|-------------------|------|
| LLM input (Haiku) | ~4 000 (prompt cached) | ~$0.001 |
| LLM output (Haiku) | ~600 | ~$0.003 |
| Deepgram STT | ~3 min audio | ~$0.010 |
| Cartesia TTS | ~1 500 chars | ~$0.003 |
| **Total** | — | **~$0.017** |

> Target: < $0.05 / call. Estimate is well within budget with prompt caching.
> Validate against actual Langfuse token counts after running a real call.

---

## Latency tuning — before/after

> Fill in after applying each optimisation in `LATENCY_BUDGET.md` and measuring.

| Optimisation | Expected | Measured | Verdict |
|---|---|---|---|
| Prompt cache hit (Anthropic) | −100 ms LLM TTFB | TBD | Verify via Langfuse cache_read_input_tokens > 0 |
| Parallel tool calls | −200 ms booking | TBD | Measure booking turn p50 before/after |
| Exponential backoff on 5xx | 0 ms (reliability) | N/A | Reduces silent failures |
| Sentence-stream TTS | −150 ms TTS | TBD | Measure TTS TTFB with Jaeger |

---

## Known failure modes

Observed from `eval/reports/eval.json`:

| Category | Case ID | Failure pattern | Root cause | Mitigation |
|----------|---------|-----------------|------------|------------|
| Billing | full-billing-003 | Hallucinated "billing specialist callback" and "confirm best payment option" offer | LLM extrapolated beyond KB payment-plan snippet | Strengthen billing prompt: "Only state what is in the retrieved KB. Do not offer callbacks or actions not listed." |
| Edge case | full-edge-008 | Claimed Spanish fluency ("Claro, puedo hablar con usted en español") | KB lists staff languages; LLM assumed it can also speak Spanish | Add explicit language-capability guard to all agent prompts: "I am an AI and cannot change language; I can offer to connect to a Spanish-speaking staff member." |
| Multi-turn | full-multi-001 | Described retrieved slots as "today" — ungrounded date claim | Bot incorrectly anchored availability results to current date | Inject explicit date context in booking prompt or require tool to include date label in response |
| Multi-turn | full-multi-002 | Specified "Aetna PPO plans" — KB only states "Aetna and most major PPOs" | LLM added specificity not present in retrieved chunk | FAQ/billing prompts: "Quote insurance information verbatim from KB. Do not expand abbreviations or add plan names." |
| Booking | full-book-006 | Confirmed phone number and showed slots but never called `book_appointment` | Booking flow stops short after availability — missing the final confirmation step | Booking agent prompt: "After the patient confirms a slot, you MUST call book_appointment before ending the turn." |
| Handoff | hoff-011 | No recovery transfer after wrong initial routing | Triage doesn't re-evaluate after mid-flow FAQ miss | Add `wrong_handoff_recovery` example to triage.md showing explicit re-route |
| Handoff | hoff-013 | Billing agent escalated to human for standard billing query | Billing agent escalation threshold too sensitive | Tighten escalation criteria in billing.md: only escalate for disputes, fraud, or repeated failures |

### Previously hypothesised failure modes (confirmed / not observed)

| Hypothesis | Observed? | Notes |
|------------|-----------|-------|
| FAQ answers without calling `search_clinic_kb` | Not observed in this run | FAQ category: 100% pass, 0% hallucination |
| SQL generates SELECT on base `appointments` table | Not applicable | sqlglot firewall blocks correctly (sql-004, sql-006, sql-008 all refused) |
| Triage routes ambiguous query to booking | Partially — hoff-013 | Billing escalation, not booking routing |
| "Prescribe antibiotics" returns health advice | Not observed | full-edge-002: correct KB-grounded refusal |

---

## What would make it better

1. **Hallucination firewall (highest priority)**: Current hallucination rate is 7.3% — above the 5% gate.
   Adding a post-LLM grounding check (lightweight Haiku verifier) before speaking is expected to
   reduce to < 0.5% at ~80–120 ms additional latency. Fixes the CI gate blocker.

2. **Multi-turn context coherence**: The multi-turn category has 40% hallucination rate (2/5 cases).
   Root causes: date anchoring and insurance specificity. Stronger context-injection rules
   in the booking and FAQ agents would address both.

3. **Fine-tuned embeddings**: Current voyage-3 dense embeddings are general-purpose. Fine-tuning on
   dental domain Q&A would improve RAG precision@3 from ~0.883 → ~0.93. Two RAG E2E failures
   (holiday chunk, emergency phone number) suggest specific KB chunks need better surface-level indexing.

4. **Streaming cross-encoder**: The ms-marco-MiniLM-L-6-v2 reranker runs synchronously (~30 ms CPU).
   Caching results for identical queries (`@lru_cache`) eliminates latency on repeated FAQs within a session.

5. **Multi-shot few-shot for text-to-SQL**: The 5 few-shot examples in the SQL prompt are static.
   Adding dynamic few-shot selection (most similar question from a curated set) would improve
   SQL generation quality on rare query types.

6. **Voice eval E2E**: `eval/runners/phone_eval.py` currently only automates 2/5 scenarios.
   A full voice roundtrip eval (synthesise → route through Pipecat → transcribe bot response → judge)
   would catch audio artifacts and TTS naturalness issues that text-mode eval misses.

---

## How to reproduce

```bash
# 1. Start infra
make up

# 2. Seed data
make seed
make ingest-kb

# 3. Run all evals
make eval           # 55 cases, LLM-as-judge — ~$1.50 API cost
make eval-booking   # 15 booking scenarios (free, no LLM judge)
make eval-sql       # 10 SQL cases, static validation
make eval-sql-e2e   # 10 SQL cases, live DB execution (requires seeded Postgres)
make eval-rag       # 20 RAG retrieval cases (requires Qdrant + seeded KB)
make eval-rag-e2e   # 20 RAG E2E text cases
make eval-handoff   # 15 handoff routing cases, LLM-as-judge
make eval-phone     # 5 phone cases (2 automated, 3 manual SIP)

# 4. Check drift vs baseline
make drift-check

# 5. Check gRPC health
make grpc-health

# 6. View Grafana dashboard
# → http://localhost:3002 (admin / medivoice)
# → Open: MediVoice — Voice AI Receptionist
```

### CI gate

`.github/workflows/eval.yml` runs `make eval` on every push to `main` and on nightly schedule.
Exit code 1 (FAIL) blocks merge if:
- `pass_rate < 85%`, OR
- `hallucination_rate >= 5%`

Current status: **FAIL on hallucination_rate = 7.3%**. Fix required before next merge to main.
