"""
Free Web Search Module for Gorpo.
No API keys required. Uses:
- Wikipedia (multi-language, requires descriptive User-Agent)
- DuckDuckGo Instant Answer API
- wttr.in for weather
"""

import re
import html
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import httpx

from bot.config import config


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Client (shared)
# ──────────────────────────────────────────────────────────────────────────────
UA = "GorpoDiscordBot/1.0 (https://github.com/gorpo-bot; bot-operator@example.com)"
_client: Optional[httpx.AsyncClient] = None


async def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": UA},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SearchResult:
    title: str
    snippet: str
    url: str
    source: str  # "wikipedia", "duckduckgo", "wttr"


# ──────────────────────────────────────────────────────────────────────────────
# Wikipedia Search
# ──────────────────────────────────────────────────────────────────────────────
async def search_wikipedia(query: str, limit: int = 5, lang: str = "en") -> List[SearchResult]:
    """Search Wikipedia and fetch summaries for top results."""
    client = await _client_get()
    base = f"https://{lang}.wikipedia.org/w/api.php"

    # 1. Search for page titles
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "format": "json",
    }
    try:
        resp = await client.get(base, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    pages = data.get("query", {}).get("search", [])
    if not pages:
        return []

    # 2. Fetch summaries for each (batch via REST API)
    results: List[SearchResult] = []
    for page in pages:
        title = page["title"]
        try:
            summary_resp = await client.get(
                f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
            )
            if summary_resp.status_code == 200:
                summary_data = summary_resp.json()
                extract = summary_data.get("extract", "")
                url = summary_data.get("content_urls", {}).get("desktop", {}).get("page", "")
                if extract:
                    results.append(SearchResult(
                        title=title,
                        snippet=extract[:500],
                        url=url or f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title)}",
                        source="wikipedia",
                    ))
        except Exception:
            continue

    return results


# ──────────────────────────────────────────────────────────────────────────────
# DuckDuckGo Instant Answer
# ──────────────────────────────────────────────────────────────────────────────
async def search_duckduckgo(query: str) -> List[SearchResult]:
    """Query DuckDuckGo Instant Answer API."""
    client = await _client_get()
    params = {
        "q": query,
        "format": "json",
        "no_html": 1,
        "skip_disambig": 1,
    }
    try:
        resp = await client.get("https://api.duckduckgo.com/", params=params)
        # DDG returns 202 for success with JSON body
        if resp.status_code not in (200, 202):
            return []
        data = resp.json()
    except Exception:
        return []

    results: List[SearchResult] = []

    # Abstract (main answer)
    abstract = data.get("Abstract", "").strip()
    abstract_url = data.get("AbstractURL", "").strip()
    if abstract:
        results.append(SearchResult(
            title=data.get("Heading", "DuckDuckGo"),
            snippet=abstract[:500],
            url=abstract_url or "https://duckduckgo.com/",
            source="duckduckgo",
        ))

    # RelatedTopics
    for topic in data.get("RelatedTopics", []):
        if isinstance(topic, dict):
            text = topic.get("Text", "").strip()
            url = topic.get("FirstURL", "").strip()
            if text:
                results.append(SearchResult(
                    title="Related",
                    snippet=text[:500],
                    url=url or "https://duckduckgo.com/",
                    source="duckduckgo",
                ))

    # Answer (direct answer if available)
    answer = data.get("Answer", "").strip()
    if answer and not any(r.snippet == answer for r in results):
        results.insert(0, SearchResult(
            title="Answer",
            snippet=answer[:500],
            url="https://duckduckgo.com/",
            source="duckduckgo",
        ))

    return results[:5]


# ──────────────────────────────────────────────────────────────────────────────
# wttr.in Weather
# ──────────────────────────────────────────────────────────────────────────────
async def search_weather(location: str) -> List[SearchResult]:
    """Fetch weather from wttr.in JSON API."""
    client = await _client_get()
    # Clean location for URL
    clean_loc = location.strip().replace(" ", "+")
    try:
        resp = await client.get(f"https://wttr.in/{clean_loc}?format=j1")
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    current = data.get("current_condition", [{}])[0]
    if not current:
        return []

    temp_c = current.get("temp_C", "?")
    temp_f = current.get("temp_F", "?")
    desc = current.get("weatherDesc", [{}])[0].get("value", "unknown")
    humidity = current.get("humidity", "?")
    wind_kmh = current.get("windspeedKmph", "?")
    feels_like_c = current.get("FeelsLikeC", "?")
    location_name = data.get("nearest_area", [{}])[0].get("areaName", [{}])[0].get("value", location)

    snippet = (
        f"{location_name}: {desc}, {temp_c}°C ({temp_f}°F), feels like {feels_like_c}°C. "
        f"Humidity: {humidity}%, Wind: {wind_kmh} km/h."
    )

    # Add 3-day forecast summary
    forecast = data.get("weather", [])[:3]
    if forecast:
        forecast_lines = []
        for day in forecast:
            date = day.get("date", "")
            max_t = day.get("maxtempC", "?")
            min_t = day.get("mintempC", "?")
            day_desc = day.get("hourly", [{}])[0].get("weatherDesc", [{}])[0].get("value", "")
            forecast_lines.append(f"  {date}: {min_t}–{max_t}°C, {day_desc}")
        snippet += "\nForecast:\n" + "\n".join(forecast_lines)

    return [SearchResult(
        title=f"Weather for {location_name}",
        snippet=snippet,
        url=f"https://wttr.in/{clean_loc}",
        source="wttr",
    )]


# ──────────────────────────────────────────────────────────────────────────────
# Heuristic Auto-Detection
# ──────────────────────────────────────────────────────────────────────────────
WEATHER_PATTERNS = [
    r"\bweather\b",
    r"\btemperature\b",
    r"\bforecast\b",
    r"\bhow (hot|cold|warm)\b",
    r"\b(climate|conditions)\b",
    r"\b(rain|raining|snow|snowing|sunny|humid)\b .*\b(today|tomorrow|now|outside|this week)\b",
]

# Only queries that genuinely require live data.
# NOTE: deliberately narrow. "what is X" / "who is X" are NOT here —
# the model decides those itself via <gorpo_search>. Broad matching here
# turns every philosophical question into a web-article summary.
REALTIME_PATTERNS = [
    r"\b(weather|forecast|temperature)\b",
    r"\b(news|headlines)\b",
    r"\b(today|tonight|tomorrow|right now|this (week|month|year)|currently)\b",
    r"\b(latest|newest|most recent|upcoming|just (released|announced|launched))\b",
    r"\b(price|stock price|share price)\b",
    r"\b(crypto|bitcoin|btc|eth|ethereum|solana|dollar|euro|usd|brl|selic|cambio|exchange rate)\b",
    r"\b(who won|final score|match result|election result)\b",
    r"\b(when (does|is|will|did) .{1,40}(release|come out|launch|premiere|air))\b",
    r"\b(release date|launch date|premiere date)\b",
    r"\b(best|top|new) .{0,30}\b(19|20)\d{2}\b",
    r"\b(aired|episode|season \d+)\b",
]


def needs_search(query: str) -> bool:
    """Heuristic: does this query likely need LIVE/real-time info?

    Intentionally conservative. Ordinary factual or creative questions
    are left to the model, which can request a search itself.
    """
    q = query.lower()
    if not any(re.search(p, q) for p in WEATHER_PATTERNS + REALTIME_PATTERNS):
        return False
    # Guard: pure philosophical / self-referential questions never auto-search
    if re.search(r"\b(meaning of life|why (are|do) we (exist|live)|purpose of life|consciousness)\b", q):
        return False
    return True


def is_weather_query(query: str) -> bool:
    q = query.lower()
    return any(re.search(pat, q) for pat in WEATHER_PATTERNS)


# ──────────────────────────────────────────────────────────────────────────────
# Main Search Orchestrator
# ──────────────────────────────────────────────────────────────────────────────
async def search_web(query: str) -> List[SearchResult]:
    """
    Main search entry point. Routes to appropriate sources based on query.
    Returns deduplicated, ranked results.
    """
    q = query.strip()
    if not q:
        return []

    # Weather → wttr.in first
    if is_weather_query(q):
        weather_results = await search_weather(q)
        # Also try Wikipedia for climate info
        wiki_results = await search_wikipedia(q, limit=3)
        return _dedup(weather_results + wiki_results)

    # General: Wikipedia + DuckDuckGo in parallel
    import asyncio
    wiki_task = search_wikipedia(q, limit=5)
    ddg_task = search_duckduckgo(q)
    wiki_results, ddg_results = await asyncio.gather(wiki_task, ddg_task, return_exceptions=True)

    results: List[SearchResult] = []
    if isinstance(wiki_results, list):
        results.extend(wiki_results)
    if isinstance(ddg_results, list):
        results.extend(ddg_results)

    return _dedup(results)


def _dedup(results: List[SearchResult]) -> List[SearchResult]:
    """Deduplicate by URL, keep first occurrence."""
    seen = set()
    unique = []
    for r in results:
        key = r.url.lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique[:8]  # Cap total results


# ──────────────────────────────────────────────────────────────────────────────
# Formatting for LLM Context
# ──────────────────────────────────────────────────────────────────────────────
def format_results_for_llm(results: List[SearchResult]) -> str:
    """Format search results as a clean block for the LLM to read."""
    if not results:
        return "🔮 The crystal ball shows nothing. No results found."

    lines = ["🔮 **Crystal Ball Results:**"]
    for i, r in enumerate(results, 1):
        source_emoji = {"wikipedia": "📖", "duckduckgo": "🦆", "wttr": "🌤️"}.get(r.source, "🔗")
        lines.append(f"\n**{i}. {r.title}** ({source_emoji} {r.source})")
        lines.append(f"   {r.snippet}")
        lines.append(f"   🔗 {r.url}")

    lines.append(
        "\n---\nThese are reference notes, not a script. Answer the user's question "
        "conversationally in your own voice, using only what is relevant. "
        "Never restructure this into a numbered article or listicle."
    )
    return "\n".join(lines)