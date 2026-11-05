# Gorpo, He-Man Mage

A self-hosted Discord AI assistant with personality. Runs on Groq (free tier), features web search via Wikipedia/DuckDuckGo/wttr.in, and music playback via yt-dlp + PyAV (zero system dependencies).

## Features

- **🧙 Persona**: Gorpo, ancient sardonic mage of Grayskull (or plain mode)
- **🤖 LLM**: Groq primary (gpt-oss-120b, gpt-oss-20b, qwen3-27b), OpenRouter fallback
- **🔮 Web Search**: Free, no API key — Wikipedia, DuckDuckGo Instant Answer, wttr.in weather
- **🎵 Music**: `/radio play <query|url>` — YouTube audio via yt-dlp + PyAV (no ffmpeg install)
- **💾 Memory**: Per-channel conversation history (JSON, atomic writes)
- **🛡️ Role Gate**: Optional role restriction (`@gorpos-master` by default)
- **⚡ Slash Commands**: `/ask`, `/search`, `/reset`, `/persona`, `/model`, `/status`, `/radio`, `/ping`, `/help`

## Quick Start (WSL Ubuntu)

```bash
# 1. Clone / navigate to project
cd /mnt/c/Users/xxxxxxPyro/gorpo

# 2. Install uv (if not present)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 3. Create venv & install deps
make install

# 4. Configure secrets
cp .env.example .env
# Edit .env with your DISCORD_TOKEN, GROQ_API_KEY, DISCORD_GUILD_ID

# 5. Start bot
make start

# 6. View logs
make logs
```

## Commands

| Command | Description |
|---------|-------------|
| `/ask <query>` | Ask Gorpo anything (auto-searches for current info) |
| `/search <query>` | Direct web search (shows raw results) |
| `/reset` | Clear this channel's conversation memory |
| `/persona <gorpo\|plain>` | Switch personality |
| `/model [provider]` | Show LLM status (auto/groq/openrouter) |
| `/status` | Bot status, limits, voice state |
| `/radio play <query\|url>` | Play music (search or YouTube URL) |
| `/radio stop` | Stop and disconnect |
| `/radio skip` | Skip current track |
| `/radio pause \| resume` | Pause/Resume |
| `/radio np` | Now playing |
| `/radio queue` | Show queue |
| `/radio volume <0-100>` | Set volume |
| `/radio leave` | Disconnect from voice |
| `/ping` | Latency check |
| `/help` | Show all commands |

## Architecture

```
gorpo/
├── bot/
│   ├── __init__.py
│   ├── config.py       # Env loading + validation
│   ├── main.py         # Bot class, event handlers, commands
│   ├── llm.py          # Groq/OpenRouter client + tool loop
│   ├── search.py       # Free web search (Wikipedia/DDG/wttr)
│   ├── music.py        # yt-dlp + PyAV voice streaming
│   ├── persona.py      # System prompts (Gorpo / Plain)
│   └── store.py        # Per-channel JSON state
├── data/               # state.json (auto-created)
├── logs/               # gorpo.log (auto-created)
├── .venv/              # uv virtual environment
├── .env                # Secrets (never commit)
├── .env.example        # Template
├── requirements.txt    # Python deps
├── Makefile            # start/stop/logs/status
└── README.md
```

## How Search Works

1. User asks: *"What's the weather in Tokyo?"*
2. Gorpo detects weather query → calls `wttr.in` API
3. Results fed back to LLM → Gorpo synthesizes: *"The spirits whisper of 18°C and light rain in Tokyo. Pack a cloak."*
4. For general queries: Wikipedia + DuckDuckGo in parallel

## Music Pipeline

```
YouTube URL / Search
       ↓
yt-dlp (bestaudio, no post-process)
       ↓
Direct stream URL (opus/webm)
       ↓
PyAV decodes → resamples to 48kHz stereo s16
       ↓
Discord.py encodes PCM → Opus (via loaded libopus from PyAV)
       ↓
Discord Voice
```

**No system ffmpeg or libopus required** — PyAV wheels ship both.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Bot token from Developer Portal |
| `GROQ_API_KEY` | Yes | Groq API key (console.groq.com) |
| `DISCORD_GUILD_ID` | Yes | Server ID for command registration |
| `GROQ_MODEL` | No | Default: `openai/gpt-oss-120b` |
| `OPENROUTER_API_KEY` | No | Fallback provider |
| `OPENROUTER_MODEL` | No | Default: `anthropic/claude-3.5-sonnet` |
| `ROLE_GATE` | No | `true`/`false` (default: true) |
| `REQUIRED_ROLE` | No | Role name for access (default: `gorpos-master`) |
| `OWNER_ID` | No | Your Discord user ID (bypasses gate) |
| `LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Discord Setup

1. **Developer Portal** → New Application → Bot
2. Enable **Message Content Intent**
3. Copy **Token** → `DISCORD_TOKEN`
4. **OAuth2 → URL Generator** → Scopes: `bot`, `applications.commands`
5. Permissions: `Send Messages`, `Read Message History`, `Embed Links`, `Attach Files`, `Add Reactions`, `Use External Emojis`, `Create Public Threads`, `Send Messages in Threads`, `Connect`, `Speak`, `Manage Messages`
6. Invite to your server
7. Create role `gorpos-master` and assign to authorized users

## Groq Setup

1. Go to [console.groq.com/keys](https://console.groq.com/keys)
2. Create API key → `GROQ_API_KEY`
3. Free tier includes: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`

## Makefile Targets

```bash
make install       # Create venv + install deps
make start         # Background start (writes gorpo.pid)
make stop          # Graceful stop (SIGTERM)
make restart       # Stop + start
make status        # Check if running
make logs          # Tail -f logs/gorpo.log
make clean         # Remove venv, logs, data, pid
make check-env     # Verify .env has required keys
make venv-recreate # Force recreate venv
```

## Running from Windows PowerShell

```powershell
# All make commands run via WSL
wsl -e bash -lc "cd /mnt/c/Users/xxxxxxPyro/gorpo && make start"
wsl -e bash -lc "cd /mnt/c/Users/xxxxxxPyro/gorpo && make logs"
wsl -e bash -lc "cd /mnt/c/Users/xxxxxxPyro/gorpo && make stop"
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `uv: command not found` | `export PATH="$HOME/.local/bin:$PATH"` or restart WSL |
| `DISCORD_TOKEN not set` | Edit `.env` with actual token |
| Bot doesn't respond | Check `make logs`; verify role gate / permissions |
| Voice fails | Ensure user in voice channel; bot has Connect/Speak perms |
| Search returns nothing | Check logs; Wikipedia blocks generic User-Agent (bot uses custom UA) |
| `ModuleNotFoundError` | Run `make install` again |

## License

MIT — Do what thou wilt.