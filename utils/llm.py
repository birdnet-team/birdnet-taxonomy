#!/usr/bin/env python3
"""
Unified LLM caller: Anthropic Claude, Google Gemini, OpenAI.

API keys are loaded from .env in the project root. Provider is auto-selected
from whichever key is present (gemini → openai → anthropic priority), or set
explicitly.

Usage:
    from utils.llm import call_llm, detect_provider

    # Auto-detect provider and model:
    text = call_llm(user="Translate to German: 'The crow is black.'")

    # Explicit:
    text = call_llm(user="...", system="Be a translator.", provider="gemini")
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

_env_cache: dict[str, str] | None = None


def _load_env() -> dict[str, str]:
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    _env_cache = env
    return env


def env_get(key: str) -> str:
    """Return a value from .env (or empty string if absent)."""
    return _load_env().get(key, "")


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

class LLMError(Exception):
    pass


# Maps provider name → (env_key, default_model)
PROVIDERS: dict[str, tuple[str, str]] = {
    "gemini":    ("GEMINI_API_KEY",    "gemini-3.1-flash-lite"),
    "openai":    ("OPENAI_API_KEY",    "gpt-4o-mini"),
    "anthropic": ("ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001"),
}

# Auto-detect priority (cheapest/fastest first for bulk translation workloads)
_DETECT_ORDER = ["gemini", "openai", "anthropic"]


def detect_provider() -> str | None:
    """Return the first provider with a key present in .env, or None."""
    for name in _DETECT_ORDER:
        env_key, _ = PROVIDERS[name]
        if env_get(env_key):
            return name
    return None


def get_api_key(provider: str) -> str:
    """Return the API key for provider from .env; raise LLMError if missing."""
    env_key, _ = PROVIDERS[provider]
    key = env_get(env_key)
    if not key:
        raise LLMError(f"{env_key} not found in .env")
    return key


# ---------------------------------------------------------------------------
# Per-provider HTTP implementations
# ---------------------------------------------------------------------------

def _call_anthropic(
    model: str, system: str, user: str, max_tokens: int, api_key: str
) -> str:
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    content = data.get("content", [])
    if content and content[0].get("type") == "text":
        return content[0]["text"].strip()
    raise LLMError(f"Unexpected Anthropic response: {data}")


def _call_gemini(
    model: str, system: str, user: str, max_tokens: int, api_key: str
) -> str:
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta"
        f"/models/{model}:generateContent"
    )
    req = Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
    )
    with urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())

    candidates = data.get("candidates", [])
    if not candidates:
        feedback = data.get("promptFeedback", {})
        reason = feedback.get("blockReason", "no candidates returned")
        raise LLMError(f"Gemini blocked: {reason}")
    parts = candidates[0].get("content", {}).get("parts", [])
    if parts and parts[0].get("text"):
        return parts[0]["text"].strip()
    finish = candidates[0].get("finishReason", "")
    raise LLMError(f"Gemini returned no text (finishReason={finish})")


def _call_openai(
    model: str, system: str, user: str, max_tokens: int, api_key: str
) -> str:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }).encode()
    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "").strip()
    raise LLMError(f"Unexpected OpenAI response: {data}")


_CALL_FN = {
    "anthropic": _call_anthropic,
    "gemini":    _call_gemini,
    "openai":    _call_openai,
}


# ---------------------------------------------------------------------------
# Public call interface
# ---------------------------------------------------------------------------

def call_llm(
    user: str,
    system: str = "",
    provider: str = "",
    model: str = "",
    max_tokens: int = 8192,
    max_retries: int = 4,
    pre_call_fn=None,
) -> str:
    """Call an LLM and return the plain-text response.

    provider:    "gemini", "openai", or "anthropic".
                 Omit (or pass "auto") to auto-detect from .env.
    model:       Overrides the provider default.
    pre_call_fn: Optional callable invoked before EVERY attempt (initial + retries).
                 Pass a rate-limiter slot-acquire function so retries also respect
                 the request delay and don't cause a retry storm.
    """
    effective_provider = provider if provider and provider != "auto" else (detect_provider() or "")
    if not effective_provider:
        raise LLMError(
            "No LLM API key found in .env. "
            "Add one of: GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY"
        )
    if effective_provider not in PROVIDERS:
        raise LLMError(
            f"Unknown provider '{effective_provider}'. "
            f"Choose from: {', '.join(PROVIDERS)}"
        )

    _, default_model = PROVIDERS[effective_provider]
    api_key = get_api_key(effective_provider)
    fn = _CALL_FN[effective_provider]
    effective_model = model or default_model

    last_exc: Exception = LLMError("no attempts made")
    for attempt in range(max_retries):
        if pre_call_fn is not None:
            pre_call_fn()
        try:
            return fn(effective_model, system, user, max_tokens, api_key)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code in (429, 500, 502, 503, 529):
                wait = min(2 ** (attempt + 1), 60)
                _warn(f"{effective_provider} {exc.code}, retry in {wait}s…")
                time.sleep(wait)
                last_exc = LLMError(f"HTTP {exc.code}: {body[:200]}")
                continue
            raise LLMError(f"HTTP {exc.code}: {body[:300]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            wait = min(2 ** (attempt + 1), 30)
            _warn(f"{effective_provider} network error, retry in {wait}s: {exc}")
            if attempt < max_retries - 1:
                time.sleep(wait)
            last_exc = exc
            continue

    raise LLMError(f"Failed after {max_retries} attempts: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# JSON response parser (shared by all callers)
# ---------------------------------------------------------------------------

def parse_json_response(text: str, label: str = "") -> dict:
    """Parse a JSON object from an LLM response, with best-effort repair."""
    if not text:
        return {}

    cleaned = text.strip()
    # Strip markdown code fences
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()

    # Extract outermost JSON object
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start == -1 or brace_end <= brace_start:
        _warn(f"No JSON object in LLM response{_loc(label)}")
        return {}
    cleaned = cleaned[brace_start : brace_end + 1]

    # Attempt 1 & 2: as-is, then trailing-comma fix
    for repair in (lambda s: s, lambda s: re.sub(r",\s*([}\]])", r"\1", s)):
        try:
            result = json.loads(repair(cleaned))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # Attempt 3: close unclosed braces
    truncated = re.sub(r",\s*([}\]])", r"\1", cleaned).rstrip().rstrip(",")
    n_open = truncated.count("{") - truncated.count("}")
    if n_open > 0:
        truncated += "}" * n_open
    try:
        result = json.loads(truncated)
        if isinstance(result, dict):
            _warn(f"Repaired truncated JSON{_loc(label)}")
            return result
    except json.JSONDecodeError:
        pass

    # Attempt 4: regex salvage of key-value string pairs
    salvaged = {}
    for m in re.finditer(r'"([^"]+)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[,}]', cleaned):
        salvaged[m.group(1)] = m.group(2).replace('\\"', '"').replace("\\n", "\n")
    if salvaged:
        _warn(f"Salvaged {len(salvaged)} entries from malformed JSON{_loc(label)}")
        return salvaged

    _warn(f"Could not parse LLM JSON ({len(text)} chars){_loc(label)}")
    return {}


def _loc(label: str) -> str:
    return f" ({label})" if label else ""


def _warn(msg: str) -> None:
    try:
        from tqdm import tqdm
        tqdm.write(f"  WARN: {msg}")
    except ImportError:
        print(f"  WARN: {msg}")
