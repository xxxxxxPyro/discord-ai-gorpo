"""
Configuration loader with validation.
All secrets live in .env (never committed).
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    # Discord
    discord_token: str
    discord_guild_id: int
    groq_api_key: str

    # LLM Providers
    groq_model: str = "openai/gpt-oss-120b"
    openrouter_api_key: Optional[str] = None
    openrouter_model: str = "anthropic/claude-3.5-sonnet"

    # Optional Discord
    owner_id: Optional[int] = None

    # Role Gate
    role_gate: bool = True
    required_role: str = "gorpos-master"

    # Limits
    cooldown_seconds: int = 5

    # Logging
    log_level: str = "INFO"

    # MongoDB Atlas (optional, for persistent state)
    mongodb_uri: str = ""
    mongodb_db_name: str = "gorpo"

    # Context / Memory
    context_token_budget: int = 50000  # max estimated tokens before oldest messages are trimmed

    @classmethod
    def load(cls) -> "Config":
        # Load from .env in project root
        project_root = Path(__file__).parent.parent.parent
        env_path = project_root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=True)
        else:
            load_dotenv(override=True)  # fallback to environment

        # Required
        discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        discord_guild_id_str = os.getenv("DISCORD_GUILD_ID", "").strip()

        if not discord_token:
            raise RuntimeError("DISCORD_TOKEN not set in .env")
        if not groq_api_key:
            raise RuntimeError("GROQ_API_KEY not set in .env")
        if not discord_guild_id_str:
            raise RuntimeError("DISCORD_GUILD_ID not set in .env")

        try:
            discord_guild_id = int(discord_guild_id_str)
        except ValueError:
            raise RuntimeError(f"DISCORD_GUILD_ID must be an integer, got: {discord_guild_id_str}")

        # Optional
        owner_id_str = os.getenv("OWNER_ID", "").strip()
        owner_id = int(owner_id_str) if owner_id_str else None

        # MongoDB Atlas (optional)
        mongodb_uri = os.getenv("MONGODB_URI", "").strip()
        mongodb_db_name = os.getenv("MONGODB_DB_NAME", "gorpo").strip()

        # Context / Memory
        context_token_budget = int(os.getenv("CONTEXT_TOKEN_BUDGET", "50000").strip())

        return cls(
            discord_token=discord_token,
            discord_guild_id=discord_guild_id,
            owner_id=owner_id,
            groq_api_key=groq_api_key,
            groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip() or None,
            openrouter_model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet").strip(),
            role_gate=os.getenv("ROLE_GATE", "true").lower() == "true",
            required_role=os.getenv("REQUIRED_ROLE", "gorpos-master").strip(),
            cooldown_seconds=int(os.getenv("COOLDOWN_SECONDS", "5")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            mongodb_uri=mongodb_uri,
            mongodb_db_name=mongodb_db_name,
            context_token_budget=context_token_budget,
        )


# Singleton instance
config = Config.load()