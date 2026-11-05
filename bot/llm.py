"""
LLM Client for Gorpo.
Supports Groq (primary) and OpenRouter (fallback).
Implements tool-use loop: model emits <gorpo_search> → bot searches → re-prompts.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, List, Optional

import httpx

log = logging.getLogger("gorpo.llm")

from bot.config import config
from bot.persona import PERSONAS, DEFAULT_PERSONA
from bot.search import search_web, format_results_for_llm, needs_search, close_client as close_search_client
from bot.store import get_channel_history, append_to_history, get_global_persona


# ──────────────────────────────────────────────────────────────────────────────
# Provider Abstraction
# ──────────────────────────────────────────────────────────────────────────────
class ProviderType(Enum):
    GROQ = "groq"
    OPENROUTER = "openrouter"


@dataclass(frozen=True)
class ProviderConfig:
    type: ProviderType
    base_url: str
    api_key: str
    model: str
    name: str


def get_groq_config() -> ProviderConfig:
    return ProviderConfig(
        type=ProviderType.GROQ,
        base_url="https://api.groq.com/openai/v1",
        api_key=config.groq_api_key,
        model=config.groq_model,
        name="Groq",
    )


def get_openrouter_config() -> Optional[ProviderConfig]:
    if not config.openrouter_api_key:
        return None
    return ProviderConfig(
        type=ProviderType.OPENROUTER,
        base_url="https://openrouter.ai/api/v1",
        api_key=config.openrouter_api_key,
        model=config.openrouter_model,
        name="OpenRouter",
    )


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Client (shared, long-lived)
# ──────────────────────────────────────────────────────────────────────────────
_llm_client: Optional[httpx.AsyncClient] = None


async def _get_llm_client() -> httpx.AsyncClient:
    global _llm_client
    if _llm_client is None or _llm_client.is_closed:
        _llm_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"User-Agent": "GorpoDiscordBot/1.0"},
        )
    return _llm_client


async def close_llm_client() -> None:
    global _llm_client
    if _llm_client and not _llm_client.is_closed:
        await _llm_client.aclose()
        _llm_client = None


# ──────────────────────────────────────────────────────────────────────────────
# Token Estimation & History Trimming
# ──────────────────────────────────────────────────────────────────────────────
# Rough: ~4 chars per token for English/code
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_history_tokens(messages: List[dict]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content", ""))
        total += 4  # role overhead
    return total


def trim_history_for_provider(
    history: List[dict],
    provider: ProviderConfig,
    system_prompt: str,
    max_new_tokens: int = 1500,
) -> List[dict]:
    """
    Trim history to fit within provider context window.
    Groq free tier: 8k context for gpt-oss-120b, but we budget conservatively.
    """
    # Context windows (conservative for free tiers)
    context_windows = {
        ProviderType.GROQ: 8192,
        ProviderType.OPENROUTER: 8192,
    }
    ctx_window = context_windows.get(provider.type, 8192)

    # Reserve tokens for system + new output + buffer
    system_tokens = estimate_tokens(system_prompt)
    reserved = system_tokens + max_new_tokens + 500

    available = ctx_window - reserved
    if available <= 0:
        return []

    # Add messages from newest to oldest until budget exhausted
    kept = []
    used = 0
    for msg in reversed(history):
        msg_tokens = estimate_tokens(msg.get("content", "")) + 4
        if used + msg_tokens > available:
            break
        kept.insert(0, msg)
        used += msg_tokens

    return kept


# ──────────────────────────────────────────────────────────────────────────────
# Temperature Heuristic
# ──────────────────────────────────────────────────────────────────────────────
TECH_PATTERNS = [
    r"\b(code|function|class|method|api|endpoint|sql|query|database|git|docker|kubernetes|config|yaml|json|xml|regex|regexp)\b",
    r"\b(error|exception|traceback|stack|bug|fix|debug|crash|fail)\b",
    r"\b(python|javascript|typescript|rust|go|java|c\+\+|c#|sql|bash|shell|powershell)\b",
    r"\b(library|framework|package|dependency|import|require|npm|pip|cargo|maven|gradle)\b",
    r"\b(async|await|promise|thread|process|memory|cpu|gpu|latency|throughput)\b",
]


def temperature_for(query: str) -> float:
    """Low temp for technical, higher for creative/chat."""
    q = query.lower()
    for pat in TECH_PATTERNS:
        if re.search(pat, q):
            return 0.25
    return 0.85


# ──────────────────────────────────────────────────────────────────────────────
# Search Tool Parsing
# ──────────────────────────────────────────────────────────────────────────────
SEARCH_TAG_RE = re.compile(r"<gorpo_search>(.*?)</gorpo_search>", re.DOTALL | re.IGNORECASE)


def extract_search_queries(text: str) -> List[str]:
    """Extract all <gorpo_search>query</gorpo_search> tags from model output."""
    return [m.strip() for m in SEARCH_TAG_RE.findall(text)]


def strip_search_tags(text: str) -> str:
    """Remove search tags from text (for display/history)."""
    return SEARCH_TAG_RE.sub("", text).strip()


# ──────────────────────────────────────────────────────────────────────────────
# Core Completion Logic
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class CompletionResult:
    text: str
    provider_used: ProviderType
    tokens_in: int
    tokens_out: int
    searches_performed: int


async def _call_provider(
    provider: ProviderConfig,
    messages: List[dict],
    temperature: float,
    max_tokens: int = 1500,
) -> tuple[str, int, int]:
    """Raw provider call. Returns (text, prompt_tokens, completion_tokens)."""
    client = await _get_llm_client()
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    # OpenRouter needs extra headers
    if provider.type == ProviderType.OPENROUTER:
        headers["HTTP-Referer"] = "https://github.com/gorpo-bot"
        headers["X-Title"] = "Gorpo Discord Bot"

    payload = {
        "model": provider.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    resp = await client.post(
        f"{provider.base_url}/chat/completions",
        headers=headers,
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    text = choice["message"]["content"] or ""
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    return text, prompt_tokens, completion_tokens


async def complete_with_tools(
    channel_id: int,
    user_query: str,
    persona_key: Optional[str] = None,
    max_search_rounds: int = 2,
) -> CompletionResult:
    """
    Main entry point: run the tool-use loop.
    1. Build messages with history + system prompt
    2. Auto-search for queries needing real-time info (weather, current events, etc.)
    3. Call provider
    4. If model emits <gorpo_search>, run search, append results, repeat
    5. Return final synthesized answer
    """
    # Resolve persona
    if persona_key is None:
        persona_key = await get_global_persona()
    persona = PERSONAS.get(persona_key, PERSONAS[DEFAULT_PERSONA])
    system_prompt = persona.system

    # Get and trim history
    history = await get_channel_history(channel_id)
    provider = get_groq_config()  # Primary provider
    trimmed_history = trim_history_for_provider(history, provider, system_prompt)

    # Build initial messages
    messages = [
        {"role": "system", "content": system_prompt},
        *trimmed_history,
    ]

    # AUTO-SEARCH: For queries that heuristically need live info,
    # run search upfront and inject results as context in user message.
    search_context = ""
    if needs_search(user_query):
        log.info("auto-search triggered: %r", user_query[:120])
        search_results = await search_web(user_query)
        if search_results:
            search_context = format_results_for_llm(search_results)
            log.info("auto-search returned %d result(s)", len(search_results))
        else:
            log.info("auto-search returned nothing; answering unaided")

    user_content = user_query
    if search_context:
        user_content = f"{search_context}\n\n---\nUser query: {user_query}"

    messages.append({"role": "user", "content": user_content})

    temperature = temperature_for(user_query)
    total_searches = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    final_text = ""
    provider_used = ProviderType.GROQ

    for round_num in range(max_search_rounds + 1):
        try:
            text, p_tok, c_tok = await _call_provider(provider, messages, temperature)
            provider_used = provider.type
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                # Rate limited - try fallback
                fallback = get_openrouter_config()
                if fallback and provider.type != ProviderType.OPENROUTER:
                    provider = fallback
                    continue  # retry with fallback
            raise
        except Exception:
            # Try fallback on any error
            fallback = get_openrouter_config()
            if fallback and provider.type != ProviderType.OPENROUTER:
                provider = fallback
                continue
            raise

        total_prompt_tokens += p_tok
        total_completion_tokens += c_tok

        # Check for search tool use
        search_queries = extract_search_queries(text)
        if search_queries:
            log.info("model requested search: %s", "; ".join(search_queries)[:200])
        if search_queries and round_num < max_search_rounds:
            # Perform searches
            search_results = []
            for sq in search_queries:
                results = await search_web(sq)
                search_results.extend(results)
                total_searches += 1

            # Format results for model
            formatted = format_results_for_llm(search_results)

            # Append model's tool call + results as context for next round
            messages.append({"role": "assistant", "content": strip_search_tags(text)})
            messages.append({"role": "user", "content": f"Search results:\n{formatted}\n\nNow synthesize a final answer."})

            # Continue loop for synthesis
            continue

        # No search tags or max rounds reached → final answer
        final_text = strip_search_tags(text)
        break

    # Save to history (user + final assistant)
    append_to_history(channel_id, "user", user_query)
    append_to_history(channel_id, "assistant", final_text)

    return CompletionResult(
        text=final_text,
        provider_used=provider_used,
        tokens_in=total_prompt_tokens,
        tokens_out=total_completion_tokens,
        searches_performed=total_searches,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Simple Completion (no tools) - for internal use
# ──────────────────────────────────────────────────────────────────────────────
async def simple_complete(prompt: str, system: str = "You are a helpful assistant.", temperature: float = 0.3) -> str:
    """One-shot completion without history or tools."""
    provider = get_groq_config()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    text, _, _ = await _call_provider(provider, messages, temperature, max_tokens=500)
    return text