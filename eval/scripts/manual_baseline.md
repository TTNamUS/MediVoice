# Manual Baseline Checklist

Use this checklist to record the 5-turn latency baseline for `docs/LATENCY_BUDGET.md`.

## Setup

1. `make up` — start Jaeger + Qdrant + Postgres
2. `make dev` — start bot server on :8000
3. `make dev-web` — start Next.js on :3000
4. Open `http://localhost:3000/clinic` in Chrome
5. Open Jaeger at `http://localhost:16686`

## Scripted turns

Click **Start Call**, wait for the greeting, then say each line in order:

| # | Say this | Expected behaviour |
|---|----------|--------------------|
| 1 | "Hi, I'd like to know your office hours." | Bot answers with clinic hours (from memory / later from RAG) |
| 2 | "Do you accept Delta Dental insurance?" | Bot answers insurance question |
| 3 | "I'd like to book a cleaning." | Bot asks for name / availability |
| 4 | "Actually, what's your cancellation policy?" | Bot answers policy |
| 5 | "That's all, thank you." | Bot closes warmly |

## Measurement

After the 5-turn call, in Jaeger:

1. Search service: `medivoice-server`
2. Find the `bot.session` trace for your session
3. Expand each `turn.stt`, `turn.llm`, `turn.tts` child span
4. Record `ttfb_ms` attribute for each turn + stage

| Turn | `turn.stt` ttfb_ms | `turn.llm` ttfb_ms | `turn.tts` ttfb_ms | Voice-to-voice |
|------|-------------------|-------------------|-------------------|----------------|
| 1    |                   |                   |                   |                |
| 2    |                   |                   |                   |                |
| 3    |                   |                   |                   |                |
| 4    |                   |                   |                   |                |
| 5    |                   |                   |                   |                |
| p50  |                   |                   |                   |                |

Copy p50 numbers into `docs/LATENCY_BUDGET.md` baseline table.

## Pass / fail

- [ ] All 5 turns completed without error
- [ ] Jaeger shows `turn.stt`, `turn.llm`, `turn.tts` spans for each turn
- [ ] Bot responses were coherent and clinic-relevant
- [ ] No pipeline crash or orphan tasks (check server logs)
