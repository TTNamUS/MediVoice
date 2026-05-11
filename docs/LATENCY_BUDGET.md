# Latency Budget — MediVoice

## Targets

| Transport      | Voice-to-voice p50 | Voice-to-voice p95 |
|----------------|-------------------|--------------------|
| LiveKit WebRTC | < 800 ms          | < 1 400 ms         |
| LiveKit SIP    | < 1 000 ms        | < 1 600 ms         |

## Stage budget breakdown (LiveKit WebRTC target)

| Stage                    | Budget   | Notes                                  |
|--------------------------|----------|----------------------------------------|
| VAD end-of-speech detect | ~150 ms  | Silero VAD; tunable via `stop_secs`    |
| STT (Deepgram nova-3)    | ~100 ms  | Streaming, first transcript chunk     |
| LLM TTFB (Haiku)         | ~250 ms  | First token; prompt-cache saves ~100ms |
| LLM generation           | ~100 ms  | Short 1–3 sentence reply              |
| TTS TTFB (Cartesia)      | ~90 ms   | Sonic-2, per Cartesia SLA             |
| Network / jitter          | ~50 ms   | LAN; +80–120 ms on PSTN (LiveKit SIP) |
| **Total (p50 estimate)** | **~740 ms** |                                     |

## Baseline measurements

> Measured manually via Jaeger span timings from a 5-turn conversation.
> Run `eval/scripts/manual_baseline.md` checklist to reproduce.

| Turn | STT TTFB | LLM TTFB | TTS TTFB | Voice-to-voice |
|------|----------|----------|----------|----------------|
| 1    | TBD      | TBD      | TBD      | TBD            |
| 2    | TBD      | TBD      | TBD      | TBD            |
| 3    | TBD      | TBD      | TBD      | TBD            |
| 4    | TBD      | TBD      | TBD      | TBD            |
| 5    | TBD      | TBD      | TBD      | TBD            |
| **p50** | –     | –        | –        | **TBD**        |
| **p95** | –     | –        | –        | **TBD**        |

*Fill in after running `make dev` + opening `/clinic` + checking Jaeger at `localhost:16686`.*

## Post-tuning delta

> Measure delta by comparing Jaeger p50 before
> and after each change — commit actual numbers here.

| Optimisation | Code location | Expected gain | Measured delta | Status |
|---|---|---|---|---|
| Anthropic prompt cache (system prompt) | `pipeline.py` `enable_prompt_caching_beta=True` | −80–150 ms LLM TTFB | TBD | ✅ enabled |
| Parallel tool calls: `lookup_patient` ∥ `check_availability` | LLM runs both tool calls in same response turn when possible | −200 ms booking turns | TBD | LLM-driven; no code change needed |
| Exponential backoff on 5xx | `bot/observability/retry.py` `@with_retry(max_attempts=3)` | 0 ms (reliability, not speed) | N/A | ✅ |
| "One moment please" on slow tools | `bot/observability/retry.py` `with_timeout_prompt(threshold=1.5s)` | Better UX on slow turns | N/A | ✅ |
| Cross-encoder cache (identical queries) | `bot/tools/rag_search.py` LRU cache on query string | −40 ms RAG on repeated queries | TBD | Implement if RAG p95 > 300 ms |
| Sentence-stream TTS | Pipecat supports this natively when LLM streams; ensure `allow_interruptions=True` | −150 ms TTS start | TBD | ✅ |

### How to measure each optimisation

```bash
# 1. Run baseline: 5 turns, note Jaeger bot.session span duration
make dev
# → open http://localhost:16686 → search service=medivoice-server → find bot.session spans

# 2. Apply one optimisation at a time
# 3. Run same 5 turns → compare p50

# Automated: grpc GetMetrics returns p50/p95 from last 5 min
make grpc-health
# then: grpcurl -plaintext localhost:50051 medivoice.MediVoice/GetMetrics
```

## LiveKit WebRTC vs LiveKit SIP comparison

> Methodology: 5 real calls per transport. STT/LLM/TTS TTFB from Jaeger spans
> (`entrypoint=browser` vs `entrypoint=sip`). Voice-to-voice = VAD end → first
> audio frame out.

| Stage                  | LiveKit WebRTC | LiveKit SIP  | Target delta | Notes |
|------------------------|-------------|--------------|--------------|-------|
| VAD end-of-speech      | ~150 ms     | ~150 ms      | ~0           | Same Silero model both paths |
| STT TTFB (Deepgram)    | TBD         | TBD          | ~0           | Same nova-3; codec-agnostic streaming |
| LLM TTFB (Haiku)       | TBD         | TBD          | ~0           | Same model, same prompt |
| TTS TTFB (Cartesia)    | TBD         | TBD          | ~0           | Same Sonic-2 |
| Network / PSTN hop     | ~50 ms      | ~80–130 ms   | +30–80 ms    | Extra PSTN→SIP→WebRTC leg |
| **Voice-to-voice p50** | **TBD**     | **TBD**      | **+30–80 ms** | Fill after real calls |
| **Voice-to-voice p95** | **TBD**     | **TBD**      | **+50–120 ms** | Higher variance on PSTN |

**Codec note**: LiveKit SIP uses Opus over its WebRTC leg — higher quality than
traditional PSTN µ-law (8 kHz, G.711). Deepgram receives clean PCM from
Pipecat's LiveKit transport; no resampling needed.

**How to measure**: after `make dev`, dial the SIP number, have a 5-turn
conversation, then open Jaeger (`http://localhost:16686`) and filter by
`transport=livekit`. Read span durations for `stt.ttfb`, `llm.ttfb`,
`tts.ttfb`, and the root `bot.session` span.

## Hypotheses for tuning

1. **Biggest win**: parallel tool calls on booking turns — `lookup_patient` and `check_availability` run sequentially today; parallelising saves ~200 ms.
2. **Second biggest**: sentence-streaming TTS — bot waits for full LLM reply today; sending first sentence to Cartesia as soon as it's complete saves ~150 ms.
3. **LLM**: Haiku already fast; switch to Sonnet only when reasoning is needed (multi-agent).
4. **Prompt cache**: effective after first call per deploy. Expect ~100 ms saved on TTFB for all cached turns.
