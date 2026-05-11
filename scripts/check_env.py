#!/usr/bin/env python3
"""Validate all required API keys and service connectivity.

Run: uv run python scripts/check_env.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Load .env.local from the server dir
env_file = Path(__file__).parent.parent / "apps" / "server" / ".env.local"
if env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(env_file)

RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"


def ok(name: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"  {GREEN}✓{RESET} {name}{suffix}")


def fail(name: str, error: str) -> None:
    print(f"  {RED}✗{RESET} {name}: {RED}{error}{RESET}")


def warn(name: str, detail: str) -> None:
    print(f"  {YELLOW}~{RESET} {name}: {YELLOW}{detail}{RESET}")


def require(var: str) -> str | None:
    val = os.getenv(var)
    if not val:
        fail(var, "not set")
        return None
    return val


# ── LLM provider checks ───────────────────────────────────────────────────────

async def check_anthropic() -> bool:
    key = require("ANTHROPIC_API_KEY")
    if not key:
        return False
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        ok("ANTHROPIC_API_KEY", f"model={msg.model}, stop={msg.stop_reason}")
        return True
    except Exception as e:
        fail("ANTHROPIC_API_KEY", str(e))
        return False


async def check_openai() -> bool:
    key = require("OPENAI_API_KEY")
    if not key:
        return False
    try:
        import httpx
        resp = httpx.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            ok("OPENAI_API_KEY", "reachable")
            return True
        else:
            fail("OPENAI_API_KEY", f"HTTP {resp.status_code}")
            return False
    except Exception as e:
        fail("OPENAI_API_KEY", str(e))
        return False


async def check_gemini() -> bool:
    key = require("GEMINI_API_KEY")
    if not key:
        return False
    try:
        import httpx
        resp = httpx.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            timeout=10,
        )
        if resp.status_code == 200:
            ok("GEMINI_API_KEY", "reachable")
            return True
        else:
            fail("GEMINI_API_KEY", f"HTTP {resp.status_code}")
            return False
    except Exception as e:
        fail("GEMINI_API_KEY", str(e))
        return False


async def check_active_llm() -> bool:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    print(f"\n  LLM_PROVIDER={BOLD}{provider}{RESET}")
    if provider == "anthropic":
        return await check_anthropic()
    if provider == "openai":
        return await check_openai()
    if provider == "gemini":
        return await check_gemini()
    fail("LLM_PROVIDER", f"unknown value '{provider}' — must be anthropic | openai | gemini")
    return False


# ── Other service checks ──────────────────────────────────────────────────────

async def check_deepgram() -> bool:
    key = require("DEEPGRAM_API_KEY")
    if not key:
        return False
    try:
        import httpx
        resp = httpx.get(
            "https://api.deepgram.com/v1/projects",
            headers={"Authorization": f"Token {key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            ok("DEEPGRAM_API_KEY", f"status={resp.status_code}")
            return True
        else:
            fail("DEEPGRAM_API_KEY", f"HTTP {resp.status_code}")
            return False
    except Exception as e:
        fail("DEEPGRAM_API_KEY", str(e))
        return False


async def check_cartesia() -> bool:
    key = require("CARTESIA_API_KEY")
    if not key:
        return False
    try:
        import httpx
        resp = httpx.get(
            "https://api.cartesia.ai/voices",
            headers={"X-API-Key": key, "Cartesia-Version": "2024-06-10"},
            timeout=10,
        )
        if resp.status_code == 200:
            voices = resp.json()
            count = len(voices) if isinstance(voices, list) else "?"
            ok("CARTESIA_API_KEY", f"{count} voices available")
            return True
        else:
            fail("CARTESIA_API_KEY", f"HTTP {resp.status_code}")
            return False
    except Exception as e:
        fail("CARTESIA_API_KEY", str(e))
        return False


async def check_livekit() -> bool:
    url = require("LIVEKIT_URL")
    key = require("LIVEKIT_API_KEY")
    secret = require("LIVEKIT_API_SECRET")
    if not url or not key or not secret:
        return False
    ok("LIVEKIT", f"url={url}")
    return True


async def check_langfuse() -> bool:
    pk = os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = os.getenv("LANGFUSE_SECRET_KEY")
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    if not pk or not sk:
        warn("LANGFUSE", "not set — OTel traces will still go to Jaeger only")
        return True
    try:
        import base64
        import httpx
        creds = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        resp = httpx.get(
            f"{host}/api/public/health",
            headers={"Authorization": f"Basic {creds}"},
            timeout=10,
        )
        if resp.status_code in (200, 307):
            ok("LANGFUSE", f"host={host}")
            return True
        else:
            fail("LANGFUSE", f"HTTP {resp.status_code}")
            return False
    except Exception as e:
        fail("LANGFUSE", str(e))
        return False


async def check_qdrant() -> bool:
    url = os.getenv("QDRANT_URL", "http://localhost:6333")
    try:
        import httpx
        resp = httpx.get(f"{url}/readyz", timeout=5)
        if resp.status_code == 200:
            ok("QDRANT", f"url={url}")
            return True
        else:
            warn("QDRANT", f"not reachable at {url}. Run: make up")
            return True
    except Exception:
        warn("QDRANT", f"not reachable at {url}. Run: make up")
        return True


async def main() -> None:
    print(f"\n{BOLD}MediVoice — environment check{RESET}")

    # Critical checks (index-ordered for the result gate below)
    critical_checks = [
        ("Active LLM", check_active_llm()),
        ("Deepgram", check_deepgram()),
        ("Cartesia", check_cartesia()),
        ("LiveKit", check_livekit()),
    ]
    # Optional checks
    optional_checks = [
        ("Langfuse", check_langfuse()),
        ("Qdrant", check_qdrant()),
    ]

    print(f"\n{BOLD}Critical:{RESET}")
    critical_results = await asyncio.gather(*(c[1] for c in critical_checks))

    print(f"\n{BOLD}Optional:{RESET}")
    await asyncio.gather(*(c[1] for c in optional_checks))

    passed = sum(critical_results)
    total = len(critical_results)
    print(f"\n{BOLD}Result: {passed}/{total} critical checks passed{RESET}")

    if not all(critical_results):
        print(f"{RED}Critical keys missing. Fill in apps/server/.env.local before starting.{RESET}\n")
        sys.exit(1)
    else:
        print(f"{GREEN}All critical keys valid. Ready to start.{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
