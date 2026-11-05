"""
Gorpo Discord Bot - Main Entry Point.
Self-hosted AI assistant with Groq, web search, and music.
"""

import asyncio
import logging
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import config
from bot.llm import (
    complete_with_tools,
    close_llm_client,
    get_groq_config,
    get_openrouter_config,
)
from bot.music import setup_music_commands
from bot.persona import PERSONAS, DEFAULT_PERSONA
from bot.search import close_client as close_search_client
from bot.store import (
    get_channel_history,
    clear_channel_history,
    get_global_persona,
    set_global_persona,
)
from bot.log_queue import LogQueue, setup_mongo_logging, log_to_mongo


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
import asyncio
import logging
from pathlib import Path

logging.basicConfig(
    level=getattr(logging, config.log_level),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gorpo")

# Quiet noisy libraries
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

# ── MongoDB Log Persistence (Option C: Async Queue) ──
# Log records are added to an async queue and periodically flushed to MongoDB
# by a background task. File-based logging (logs/gorpo.log) remains the primary
# method; MongoDB persistence is best-effort supplementary.

_mongo_client_for_logs = None
_log_queue: Optional[LogQueue] = None
_log_flush_task: Optional[asyncio.Task] = None


async def _mongo_log_init() -> None:
    """Initialise MongoDB log queue and background flusher."""
    global _mongo_client_for_logs, _log_queue, _log_flush_task

    if _log_queue is not None:
        return  # already initialised

    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        _mongo_client_for_logs = AsyncIOMotorClient(
            config.mongodb_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
        )
        # Test connection
        await _mongo_client_for_logs.admin.command("ping")

        # Set up queue + flusher
        _log_queue, _log_flush_task = setup_mongo_logging(_mongo_client_for_logs, config.mongodb_db_name)
        log.info("MongoDB log queue initialised (Option C: async queue flush)")

    except Exception as e:
        log.warning("MongoDB log queue unavailable: %s", e)
        _log_queue = None
        _log_flush_task = None


# ── End MongoDB Log Persistence ──────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# Bot Class
# ──────────────────────────────────────────────────────────────────────────────
class GorpoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        intents.voice_states = True

        super().__init__(
            command_prefix="!",  # Unused, slash-only
            intents=intents,
            help_command=None,
        )

        self.tree: app_commands.CommandTree
        self.synced = False
        self._busy_channels: set[int] = set()
        self._last_asked: dict[int, float] = {}  # user_id -> timestamp
        self._start_time: float = 0

    async def setup_hook(self) -> None:
        # Register music commands
        setup_music_commands(self.tree)

        # Register core commands using decorator-style on self.tree
        @self.tree.command(name="ask", description="Ask Gorpo a question (uses web search if needed)")
        @app_commands.describe(query="Your question")
        async def ask_cmd(interaction: discord.Interaction, query: str):
            await interaction.response.defer(thinking=True, ephemeral=False)
            await self.process_ask(
                interaction.channel, interaction.user, query, interaction=interaction
            )

        @self.tree.command(name="reset", description="Clear this channel's conversation memory")
        async def reset_cmd(interaction: discord.Interaction):
            if not self.has_access(
                interaction.user if isinstance(interaction.user, discord.Member) else None,
                interaction.user.id,
            ):
                await interaction.response.send_message("🚫 Access denied.", ephemeral=True)
                return

            clear_channel_history(interaction.channel_id)
            await interaction.response.send_message("🧹 Channel memory cleared. The slate is clean.")

        @self.tree.command(name="model", description="Show LLM provider status")
        @app_commands.describe(provider="Provider to use")
        @app_commands.choices(provider=[
            app_commands.Choice(name="Groq (primary)", value="groq"),
            app_commands.Choice(name="OpenRouter (fallback)", value="openrouter"),
            app_commands.Choice(name="Auto (Groq → OpenRouter)", value="auto"),
        ])
        async def model_cmd(interaction: discord.Interaction, provider: str = "auto"):
            groq = get_groq_config()
            or_conf = get_openrouter_config()

            lines = [
                f"**Current Mode:** `{provider}`",
                f"**Primary:** {groq.name} — `{groq.model}`",
            ]
            if or_conf:
                lines.append(f"**Fallback:** {or_conf.name} — `{or_conf.model}`")
            else:
                lines.append("**Fallback:** Not configured")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="search", description="Direct web search (shows raw results)")
        @app_commands.describe(query="Search query")
        async def search_cmd(interaction: discord.Interaction, query: str):
            await interaction.response.defer(ephemeral=False)
            from bot.search import search_web, format_results_for_llm
            results = await search_web(query)
            formatted = format_results_for_llm(results)
            await interaction.followup.send(formatted[:1900])

        @self.tree.command(name="status", description="Show bot status, limits, and current config")
        async def status_cmd(interaction: discord.Interaction):
            if not self.has_access(
                interaction.user if isinstance(interaction.user, discord.Member) else None,
                interaction.user.id,
            ):
                await interaction.response.send_message("🚫 Access denied.", ephemeral=True)
                return

            groq = get_groq_config()
            or_conf = get_openrouter_config()
            persona_key = get_global_persona()
            persona = PERSONAS.get(persona_key, PERSONAS[DEFAULT_PERSONA])

            vc_status = "Disconnected"
            vc = interaction.guild.voice_client
            if vc and vc.is_connected():
                vc_status = f"Connected to {vc.channel.name}"
                if vc.is_playing():
                    vc_status += " 🎵 Playing"
                elif vc.is_paused():
                    vc_status += " ⏸️ Paused"

            uptime = int(asyncio.get_event_loop().time() - self._start_time)
            h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60

            lines = [
                f"**Gorpo Status**",
                f"👤 User: {self.user} (ID: {self.user.id})",
                f"⏱️ Uptime: {h}h {m}m {s}s",
                "",
                f"**Oracles:**",
                f"  • {groq.name}: `{groq.model}`",
            ]
            if or_conf:
                lines.append(f"  • {or_conf.name}: `{or_conf.model}`")
            else:
                lines.append(f"  • OpenRouter: Not configured")

            lines += [
                "",
                f"**Persona:** {persona.name} (`{persona_key}`)",
                f"**Role Gate:** {'ON' if config.role_gate else 'OFF'} (requires `@{config.required_role}`)",
                "",
                f"**Limits:**",
                f"  • History (local): {config.max_history_local} messages",
                f"  • History (cloud): {config.max_history_cloud} messages",
                f"  • Cloud token budget: {config.cloud_token_budget:,}",
                f"  • Cooldown: {config.cooldown_seconds}s per user",
                "",
                f"**Voice:** {vc_status}",
            ]

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="persona", description="Switch Gorpo's personality")
        @app_commands.describe(mode="Persona mode")
        @app_commands.choices(mode=[
            app_commands.Choice(name="Gorpo (mage)", value="gorpo"),
            app_commands.Choice(name="Plain (direct)", value="plain"),
        ])
        async def persona_cmd(interaction: discord.Interaction, mode: str):
            if mode not in PERSONAS:
                await interaction.response.send_message(f"Unknown persona: {mode}", ephemeral=True)
                return

            set_global_persona(mode)
            persona = PERSONAS[mode]
            await interaction.response.send_message(f"🎭 Persona set to **{persona.name}** — {persona.description}")

        @self.tree.command(name="ping", description="Check bot latency")
        async def ping_cmd(interaction: discord.Interaction):
            latency = self.latency * 1000
            await interaction.response.send_message(f"🏓 Pong! `{latency:.0f}ms`", ephemeral=True)

        @self.tree.command(name="help", description="Show all available commands")
        async def help_cmd(interaction: discord.Interaction):
            lines = [
                "**Gorpo — Commands**",
                "",
                "**Chat & AI:**",
                "`/ask <query>` — Ask a question (auto-searches if needed)",
                "`/search <query>` — Direct web search (raw results)",
                "`/reset` — Clear this channel's memory",
                "`/persona <gorpo|plain>` — Switch personality",
                "`/model [provider]` — Show LLM status",
                "",
                "**Music (Radio):**",
                "`/radio play <query|url>` — Play song (search or YouTube URL)",
                "`/radio stop` — Stop and disconnect",
                "`/radio skip` — Skip current",
                "`/radio pause` / `/radio resume` — Pause/Resume",
                "`/radio np` — Now playing",
                "`/radio queue` — Show queue",
                "`/radio volume <0-100>` — Set volume",
                "`/radio leave` — Disconnect",
                "",
                "**Meta:**",
                "`/status` — Bot status & config",
                "`/ping` — Latency check",
                "`/help` — This message",
                "",
                f"**Role Gate:** {'Enabled' if config.role_gate else 'Disabled'} (requires `@{config.required_role}`)",
            ]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        # Sync to guild
        guild = discord.Object(id=config.discord_guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.synced = True
        log.info("Slash commands synced to guild %s", config.discord_guild_id)

    async def on_ready(self) -> None:
        self._start_time = asyncio.get_event_loop().time()
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Oracle: %s (%s)", get_groq_config().model, get_groq_config().name)

        fallback = get_openrouter_config()
        if fallback:
            log.info("Fallback: %s (%s)", fallback.model, fallback.name)

        # Set presence
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/ask or mention me",
            )
        )

    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots and self
        if message.author.bot or message.author == self.user:
            return

        # Check if bot is mentioned
        if self.user in message.mentions:
            await self.handle_mention(message)

    async def handle_mention(self, message: discord.Message) -> None:
        # Strip mention
        content = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        if not content:
            await message.reply("Yes, mortal? Speak your query.", mention_author=False)
            return

        # Delegate to ask logic
        ctx = await self.get_context(message)
        # Create a pseudo-interaction for unified handling
        await self.process_ask(message.channel, message.author, content, is_mention=True, reference=message)

    async def process_ask(
        self,
        channel: discord.abc.Messageable,
        user: discord.User,
        query: str,
        is_mention: bool = False,
        reference: discord.Message = None,
        interaction: discord.Interaction = None,
    ) -> None:
        # Role gate
        if config.role_gate:
            member = None
            if interaction:
                member = interaction.user if isinstance(interaction.user, discord.Member) else None
                log.debug(f"SLASH CMD GUILD | guild={interaction.guild.id} ({interaction.guild.name}) | user={user.id}")
            elif isinstance(channel, discord.TextChannel):
                member = channel.guild.get_member(user.id)
                log.debug(f"MENTION GUILD | channel={channel.id} | guild={channel.guild.id} ({channel.guild.name}) | user={user.id} | member={member}")

            if not self.has_access(member, user.id):
                msg = "🚫 The tower gates are closed. You lack the required role."
                if interaction:
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await channel.send(msg, reference=reference, mention_author=False)
                return

        # Cooldown
        now = asyncio.get_event_loop().time()
        last = self._last_asked.get(user.id, 0)
        if now - last < config.cooldown_seconds:
            remaining = int(config.cooldown_seconds - (now - last))
            msg = f"⏳ Patience, mortal. Wait {remaining}s before consulting the tower again."
            if interaction:
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await channel.send(msg, reference=reference, mention_author=False)
            return

        # Busy guard per channel
        channel_id = channel.id if hasattr(channel, "id") else 0
        if channel_id in self._busy_channels:
            msg = "🔮 The crystal ball is busy. One query at a time per channel."
            if interaction:
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await channel.send(msg, reference=reference, mention_author=False)
            return

        self._last_asked[user.id] = now
        self._busy_channels.add(channel_id)

        # Send "thinking" indicator
        thinking_msg = None
        if not interaction:
            thinking_msg = await channel.send("🔮 Consulting the stars...", reference=reference, mention_author=False)

        try:
            # Call LLM with tools
            result = await complete_with_tools(channel_id, query)

            # Split long responses
            chunks = self.chunk_response(result.text)
            for i, chunk in enumerate(chunks):
                if interaction:
                    if i == 0:
                        await interaction.followup.send(chunk)
                    else:
                        await interaction.followup.send(chunk)
                else:
                    if i == 0 and thinking_msg:
                        await thinking_msg.edit(content=chunk)
                    else:
                        await channel.send(chunk)

        except Exception as e:
            log.exception("Error processing ask")
            err_msg = self.format_error(e)
            if interaction:
                await interaction.followup.send(err_msg, ephemeral=True)
            elif thinking_msg:
                await thinking_msg.edit(content=err_msg)
            else:
                await channel.send(err_msg, reference=reference, mention_author=False)
        finally:
            self._busy_channels.discard(channel_id)

    def has_access(self, member: discord.Member | None, user_id: int) -> bool:
        if not config.role_gate:
            return True
        if config.owner_id and user_id == config.owner_id:
            return True
        if member is None:
            log.debug(f"ACCESS DENIED | user={user_id} | member=None (DM or intents missing)")
            return False  # DMs denied
        if member.guild.owner_id == user_id:
            return True
        if member.guild_permissions.administrator:
            log.debug(f"ACCESS GRANTED | user={user_id} | admin=True")
            return True
        role_names = {r.name.lower() for r in member.roles}
        has_role = config.required_role.lower() in role_names
        log.debug(f"ACCESS CHECK | user={user_id} | required_role={config.required_role!r} | member_roles={[r.name for r in member.roles]} | has_role={has_role}")
        if not has_role:
            log.debug(f"ACCESS DENIED | user={user_id} | role_missing | required={config.required_role!r} | available={[r.name.lower() for r in member.roles]}")
        return has_role

    def chunk_response(self, text: str, limit: int = 1900) -> list[str]:
        """Split response at paragraph boundaries, preserving code fences."""
        if len(text) <= limit:
            return [text]

        chunks = []
        current = ""
        in_code = False
        code_lang = ""

        for line in text.split("\n"):
            # Track code fence state
            if line.startswith("```"):
                if not in_code:
                    in_code = True
                    code_lang = line[3:].strip()
                else:
                    in_code = False
                    code_lang = ""

            # Check if adding this line would exceed limit
            if len(current) + len(line) + 1 > limit:
                if in_code:
                    # Close code block, start new chunk with reopened fence
                    current += "\n```"
                    chunks.append(current)
                    current = f"```{code_lang}\n{line}"
                else:
                    chunks.append(current)
                    current = line
            else:
                if current:
                    current += "\n"
                current += line

        if current:
            chunks.append(current)

        return chunks

    def format_error(self, error: Exception) -> str:
        """Gorpo-style error messages."""
        err_str = str(error).lower()
        if "rate limit" in err_str or "429" in err_str:
            return "⚡ The free quota is exhausted. The stars are silent until the moon turns."
        if "timeout" in err_str:
            return "🌑 The crystal ball timed out. The spirits are slow today."
        if "unauthorized" in err_str or "401" in err_str:
            return "🔐 The tower's key is invalid. Check the secrets, mortal."
        if "connection" in err_str or "connect" in err_str:
            return "🌫️ The ether is disturbed. Cannot reach the oracle."
        return f"💥 The tower trembles: `{type(error).__name__}`"


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    bot = GorpoBot()
    try:
        await bot.start(config.discord_token)
    except KeyboardInterrupt:
        log.info("Shutdown requested")
    except Exception:
        log.exception("Fatal error")
    finally:
        # Clean up MongoDB log queue
        if _log_flush_task is not None:
            _log_flush_task.cancel()
        if _mongo_client_for_logs is not None:
            _mongo_client_for_logs.close()
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass