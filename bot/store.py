"""Per-channel conversation state persistence with MongoDB Atlas.

Supports per-channel history and global persona state.
Falls back gracefully if MongoDB is unavailable.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import logging
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConnectionFailure, OperationFailure

from bot.config import config

log = logging.getLogger("gorpo.store")


def estimate_tokens(text: str) -> int:
    """Estimate token count: ~4 chars per token, minimum 1."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_history_tokens(messages: List[Dict[str, str]]) -> int:
    """Estimate total tokens for a list of {role, content} messages."""
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content", ""))
        total += 4  # role overhead (user/assistant)
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Token-Aware Pruning (oldest-first trim)
# ──────────────────────────────────────────────────────────────────────────────
def _trim_history_by_token_budget(
    history: List[Dict[str, str]],
    max_tokens: int,
    min_messages: int = 2,
) -> List[Dict[str, str]]:
    """
    Trim history from the oldest end until token budget is satisfied.

    Parameters
    ----------
    history : List[Dict[str, str]]
        List of {role, content} messages, stored newest-first.
    max_tokens : int
        Maximum combined token budget (including role overhead).
    min_messages : int
        Minimum number of messages to retain (default 2 = one user + one assistant).

    Returns
    -------
    List[Dict[str, str]]
        Trimmed history, newest messages preserved, oldest removed.
    """
    if not history:
        return history

    # If already within budget, nothing to do
    current_tokens = estimate_history_tokens(history)
    if current_tokens <= max_tokens:
        return history

    # Need to trim: start from the oldest (end of list) and remove
    # Keep at least min_messages
    trimmed = list(history)

    while len(trimmed) > min_messages and estimate_history_tokens(trimmed) > max_tokens:
        # Remove the oldest message (last in list, since we store newest-first)
        trimmed.pop()

    # Final check: ensure we're within budget, but keep at least min_messages
    while estimate_history_tokens(trimmed) > max_tokens and len(trimmed) > min_messages:
        trimmed.pop()

    return trimmed

# ──────────────────────────────────────────────────────────────────────────────
# Token Estimation
# ──────────────────────────────────────────────────────────────────────────────
# Rough: ~4 chars per token for English/code (same as llm.py CHARS_PER_TOKEN)
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count: ~4 chars per token, minimum 1."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_history_tokens(messages: List[Dict[str, str]]) -> int:
    """Estimate total tokens for a list of {role, content} messages."""
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content", ""))
        total += 4  # role overhead (user/assistant)
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Token-Aware Pruning
# ──────────────────────────────────────────────────────────────────────────────
def _trim_history_by_token_budget(
    history: List[Dict[str, str]],
    max_tokens: int,
    min_messages: int = 2,
) -> List[Dict[str, str]]:
    """
    Trim history from the oldest end until token budget is satisfied.
    
    Parameters
    ----------
    history : List[Dict[str, str]]
        List of {role, content} messages, newest first or oldest first.
        This function assumes newest-first order (as stored by append_to_history).
    max_tokens : int
        Maximum combined token budget (including role overhead).
    min_messages : int
        Minimum number of messages to retain (default 2 = one user + one assistant).
    
    Returns
    -------
    List[Dict[str, str]]
        Trimmed history, newest messages preserved, oldest removed.
    """
    if not history:
        return history

    # Calculate current token usage
    current_tokens = estimate_history_tokens(history)

    # If already within budget, nothing to do
    if current_tokens <= max_tokens:
        return history

    # Need to trim: start from the oldest (end of list) and remove
    # Keep at least min_messages
    trim_until = max(min_messages, len(history) - 1)  # leave at least min_messages

    trimmed = list(history)  # copy
    while len(trimmed) > trim_until and estimate_history_tokens(trimmed) > max_tokens:
        # Remove the oldest message (last in list, since we store newest-first)
        trimmed.pop()

    # Recalculate and ensure we're within budget
    while estimate_history_tokens(trimmed) > max_tokens and len(trimmed) > min_messages:
        trimmed.pop()

    return trimmed


# ──────────────────────────────────────────────────────────────────────────────
# Provider Token Budgets (from config)
# ──────────────────────────────────────────────────────────────────────────────
# These are rough token budgets per provider per conversation.
# The store enforces context_token_budget (message-cap-free, token-based only) when pruning is active.
# ──────────────────────────────────────────────────────────────────────────────

# Default token budgets (can be overridden per-call)

# ──────────────────────────────────────────────────────────────────────────────
# MongoDB Client (lazy‑initialised)
# ──────────────────────────────────────────────────────────────────────────────
_mongo_client: Optional[AsyncIOMotorClient] = None
_mongo_db = None
_mongo_channels_col = None
_mongo_persona_col = None


async def _ensure_mongo() -> None:
    """Initialise the MongoDB client and collections if not already done."""
    global _mongo_client, _mongo_db, _mongo_channels_col, _mongo_persona_col

    if _mongo_client is not None:
        return  # already initialised

    try:
        _mongo_client = AsyncIOMotorClient(
            config.mongodb_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
        )
        # Test connection
        await _mongo_client.admin.command("ping")
        log.info("MongoDB Atlas ping succeeded")

        _mongo_db = _mongo_client[config.mongodb_db_name]
        _mongo_channels_col = _mongo_db["channel_history"]
        _mongo_persona_col = _mongo_db["persona_state"]

        # Ensure indexes for performance
        await _mongo_channels_col.create_index("channel_id", unique=False)
        await _mongo_persona_col.create_index("key", unique=True)

        log.info("MongoDB collections ready: %s.%s", config.mongodb_db_name, "channel_history")

    except ConnectionFailure as e:
        log.error("MongoDB connection failed: %s", e)
        _mongo_client = None
    except OperationFailure as e:
        log.error("MongoDB authentication/error failed: %s", e)
        _mongo_client = None


# ──────────────────────────────────────────────────────────────────────────────
# Fallback to JSON (legacy) — used only if MongoDB is down
# ──────────────────────────────────────────────────────────────────────────────
import json
import os
import threading
from pathlib import Path

_JSON_STATE_FILE = Path(__file__).parent.parent.parent / "data" / "state.json"
_lock = threading.RLock()


def _ensure_json_dir() -> None:
    _JSON_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(data: Dict[str, Any]) -> None:
    _ensure_json_dir()
    tmp = _JSON_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _JSON_STATE_FILE)


def _load_json() -> Dict[str, Any]:
    if not _JSON_STATE_FILE.exists():
        return {"channels": {}, "persona": "gorpo"}
    try:
        with open(_JSON_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"channels": {}, "persona": "gorpo"}


def _save_json(state: Dict[str, Any]) -> None:
    with _lock:
        _atomic_write_json(state)


# ──────────────────────────────────────────────────────────────────────────────
# Primary MongoDB-backed functions (with JSON fallback)
# ──────────────────────────────────────────────────────────────────────────────


async def _init_mongo_if_needed() -> None:
    """Call once at startup; ensures MongoDB client is alive."""
    await _ensure_mongo()


# ── Channel history ──────────────────────────────────────────────────────────

async def load_state() -> Dict[str, Any]:
    """Load all channel history + persona from MongoDB (fallback to JSON)."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        log.warning("MongoDB unavailable; falling back to JSON state")
        return _load_json()

    try:
        # Load channel history
        channels: Dict[str, List[Dict[str, str]]] = {}
        async for doc in _mongo_channels_col.find({"channel_id": {"$exists": True}}):
            cid = str(doc.get("channel_id"))
            history = doc.get("history", [])
            # Normalise to list of {role, content}
            norm = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in history
            ]
            channels[cid] = norm

        # Load persona
        persona_doc = await _mongo_persona_col.find_one({"key": "current"})
        persona = persona_doc["value"] if persona_doc else "gorpo"

        result = {"channels": channels, "persona": persona}

        # Also merge with JSON for any channels not yet in MongoDB
        json_state = _load_json()
        for cid_str, history in json_state.get("channels", {}).items():
            if cid_str not in channels:
                channels[cid_str] = history

        return result

    except Exception as e:
        log.exception("Error loading state from MongoDB")
        return _load_json()


async def save_state(state: Dict[str, Any]) -> None:
    """Persist all channel history + persona to MongoDB (with JSON fallback)."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        log.warning("MongoDB unavailable; falling back to JSON state")
        _save_json(state)
        return

    try:
        channels = state.get("channels", {})
        persona = state.get("persona", "gorpo")

        # Upsert each channel's history
        for cid_str, history in channels.items():
            # Normalise history to list of {role, content}
            norm = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in (history or [])
            ]
            await _mongo_channels_col.update_one(
                {"channel_id": int(cid_str) if cid_str.isdigit() else cid_str},
                {"$set": {"history": norm}},
                upsert=True,
            )

        # Upsert persona
        await _mongo_persona_col.update_one(
            {"key": "current"},
            {"$set": {"value": persona}},
            upsert=True,
        )

        log.info("State persisted to MongoDB (%d channels)", len(channels))

    except Exception as e:
        log.exception("Error saving state to MongoDB")
        _save_json(state)


async def get_channel_history(channel_id: int) -> List[Dict[str, str]]:
    """Return the conversation history for a given channel."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        state = _load_json()
        return state.get("channels", {}).get(str(channel_id), [])

    try:
        doc = await _mongo_channels_col.find_one({"channel_id": channel_id})
        if doc and doc.get("history"):
            return doc["history"]
        return []
    except Exception as e:
        log.exception("Error fetching channel history")
        return []


async def set_channel_history(channel_id: int, history: List[Dict[str, str]]) -> None:
    """Set the conversation history for a given channel."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        state = _load_json()
        state.setdefault("channels", {})[str(channel_id)] = history
        _save_json(state)
        return

    try:
        await _mongo_channels_col.update_one(
            {"channel_id": channel_id},
            {"$set": {"history": history}},
            upsert=True,
        )
    except Exception as e:
        log.exception("Error setting channel history")


async def append_to_history(channel_id: int, role: str, content: str) -> None:
    """Append a single turn to a channel's history with token-aware pruning."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        state = _load_json()
        history = state.setdefault("channels", {}).setdefault(str(channel_id), [])
        history.append({"role": role, "content": content})
        # Token-aware pruning: trim oldest messages if over budget
        hist_tokens = estimate_history_tokens(history)
        if hist_tokens > config.context_token_budget:
            history = _trim_history_by_token_budget(history, config.context_token_budget, min_messages=2)
        state["channels"][str(channel_id)] = history
        _save_json(state)
        return

    try:
        doc = await _mongo_channels_col.find_one({"channel_id": channel_id})
        history: List[Dict[str, str]] = []
        if doc and doc.get("history"):
            history = doc["history"]

        history.append({"role": role, "content": content})
        # Token-aware pruning: trim oldest messages if over budget
        hist_tokens = estimate_history_tokens(history)
        if hist_tokens > config.context_token_budget:
            history = _trim_history_by_token_budget(history, config.context_token_budget, min_messages=2)

        await _mongo_channels_col.update_one(
            {"channel_id": channel_id},
            {"$set": {"history": history}},
            upsert=True,
        )
    except Exception as e:
        log.exception("Error appending to channel history")


async def clear_channel_history(channel_id: int) -> None:
    """Clear the conversation history for a given channel."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        state = _load_json()
        channels = state.setdefault("channels", {})
        if str(channel_id) in channels:
            del channels[str(channel_id)]
            _save_json(state)
        return

    try:
        await _mongo_channels_col.delete_one({"channel_id": channel_id})
    except Exception as e:
        log.exception("Error clearing channel history")


async def prune_channel_history(channel_id: int, max_tokens: Optional[int] = None) -> Dict[str, Any]:
    """
    Prune a channel's history to fit within a token budget.
    
    Parameters
    ----------
    channel_id : int
        The Discord channel ID to prune.
    max_tokens : Optional[int]
        Maximum token budget. If None, uses `config.context_token_budget` (50000).
    
    Returns
    -------
    Dict[str, Any]
        {
            "pruned": bool,
            "before_tokens": int,
            "after_tokens": int,
            "messages_removed": int,
            "remaining_messages": int,
        }
    """
    if max_tokens is None:
        max_tokens = config.context_token_budget

    await _init_mongo_if_needed()

    if _mongo_client is None:
        state = _load_json()
        history = state.get("channels", {}).get(str(channel_id), [])
    else:
        doc = await _mongo_channels_col.find_one({"channel_id": channel_id})
        history = doc["history"] if doc and doc.get("history") else []

    before_tokens = estimate_history_tokens(history)
    before_count = len(history)

    # Trim using the budget function
    trimmed = _trim_history_by_token_budget(history, max_tokens, min_messages=2)

    after_tokens = estimate_history_tokens(trimmed)
    after_count = len(trimmed)
    messages_removed = before_count - after_count

    # Write back if changed
    if messages_removed > 0:
        if _mongo_client is None:
            state["channels"][str(channel_id)] = trimmed
            _save_json(state)
        else:
            await _mongo_channels_col.update_one(
                {"channel_id": channel_id},
                {"$set": {"history": trimmed}},
                upsert=True,
            )

    return {
        "pruned": messages_removed > 0,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "messages_removed": messages_removed,
        "remaining_messages": after_count,
    }


# ── Global persona ───────────────────────────────────────────────────────────

async def get_global_persona() -> str:
    """Return the current global persona key."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        state = _load_json()
        return state.get("persona", "gorpo")

    try:
        doc = await _mongo_persona_col.find_one({"key": "current"})
        return doc["value"] if doc else "gorpo"
    except Exception as e:
        log.exception("Error fetching global persona")
        return "gorpo"


async def set_global_persona(persona_key: str) -> None:
    """Set the current global persona key."""
    await _init_mongo_if_needed()

    if _mongo_client is None:
        state = _load_json()
        state["persona"] = persona_key
        _save_json(state)
        return

    try:
        await _mongo_persona_col.update_one(
            {"key": "current"},
            {"$set": {"value": persona_key}},
            upsert=True,
        )
    except Exception as e:
        log.exception("Error setting global persona")


# ── Startup / shutdown ───────────────────────────────────────────────────────

async def startup() -> None:
    """Initialise MongoDB connection when the bot starts."""
    await _ensure_mongo()


async def shutdown() -> None:
    """Close MongoDB connection when the bot stops."""
    global _mongo_client
    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None
        log.info("MongoDB connection closed")