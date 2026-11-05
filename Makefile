# Gorpo Discord Bot - Makefile
# Run from WSL: make <target>
# All commands assume project root: /mnt/c/Users/xxxxxxPyro/gorpo

SHELL := /bin/bash
PROJECT_ROOT := /mnt/c/Users/xxxxxxPyro/gorpo
VENV := $(PROJECT_ROOT)/.venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UV := $(HOME)/.local/bin/uv

# Export PATH for uv
export PATH := $(HOME)/.local/bin:$(PATH)

.PHONY: help install build start stop restart status logs clean download-ffmpeg check-env venv-recreate

# Default target
help:
	@echo "Gorpo Discord Bot — Make Targets"
	@echo ""
	@echo "  make install       - Create venv (uv) and install dependencies"
	@echo "  make build         - No-op (Python), kept for compatibility"
	@echo "  make start         - Start bot in background (writes gorpo.pid, logs to logs/gorpo.log)"
	@echo "  make stop          - Stop bot gracefully (SIGTERM via pid file)"
	@echo "  make restart       - Stop then start"
	@echo "  make status        - Show if bot is running"
	@echo "  make logs          - Tail the log file (Ctrl-C to exit)"
	@echo "  make clean         - Remove venv, logs, data, pid, cache"
	@echo "  make download-ffmpeg - Download static ffmpeg (optional, PyAV bundles it)"
	@echo "  make check-env     - Verify .env exists and has required keys"
	@echo "  make venv-recreate - Force recreate venv (fixes corruption)"
	@echo ""

# Create venv and install deps
install: $(VENV)/bin/activate
	@echo "✅ Environment ready"

$(VENV)/bin/activate: requirements.txt
	@echo "📦 Creating venv with uv..."
	$(UV) venv --clear --python 3.14 $(VENV)
	@echo "📥 Installing dependencies..."
	$(UV) pip install --python $(PYTHON) -r $(PROJECT_ROOT)/requirements.txt
	@touch $(VENV)/bin/activate

build:
	@echo "ℹ️  Python project — no build step needed"

# Start bot in background (delegates to run_bot.sh)
start: check-env
	@$(PROJECT_ROOT)/scripts/run_bot.sh start

# Stop bot gracefully (delegates to run_bot.sh)
stop:
	@$(PROJECT_ROOT)/scripts/run_bot.sh stop

restart: stop start

status:
	@$(PROJECT_ROOT)/scripts/run_bot.sh status

logs:
	@$(PROJECT_ROOT)/scripts/run_bot.sh logs

clean:
	@echo "🧹 Cleaning..."
	@rm -rf $(VENV)
	@rm -rf $(PROJECT_ROOT)/logs
	@rm -rf $(PROJECT_ROOT)/data
	@rm -f $(PROJECT_ROOT)/gorpo.pid
	@rm -rf $(PROJECT_ROOT)/__pycache__
	@rm -rf $(PROJECT_ROOT)/bot/__pycache__
	@rm -rf $(PROJECT_ROOT)/.uv_cache
	@echo "✅ Cleaned"

download-ffmpeg:
	@echo "ℹ️  PyAV bundles FFmpeg — no system ffmpeg needed."
	@echo "   If you really want a static binary:"
	@echo "   wget -O ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
	@echo "   tar -xf ffmpeg.tar.xz --strip-components=1 -C $(VENV)/bin ffmpeg-*/ffmpeg"
	@echo "   chmod +x $(VENV)/bin/ffmpeg"

check-env:
	@if [ ! -f $(PROJECT_ROOT)/.env ]; then \
		echo "❌ .env not found! Copy .env.example to .env and fill in secrets."; \
		exit 1; \
	fi
	@echo "✅ .env exists"
	@grep -q "^DISCORD_TOKEN=" $(PROJECT_ROOT)/.env && echo "  ✓ DISCORD_TOKEN" || echo "  ✗ DISCORD_TOKEN missing"
	@grep -q "^GROQ_API_KEY=" $(PROJECT_ROOT)/.env && echo "  ✓ GROQ_API_KEY" || echo "  ✗ GROQ_API_KEY missing"
	@grep -q "^DISCORD_GUILD_ID=" $(PROJECT_ROOT)/.env && echo "  ✓ DISCORD_GUILD_ID" || echo "  ✗ DISCORD_GUILD_ID missing"

venv-recreate:
	@echo "🔄 Recreating venv..."
	@rm -rf $(VENV)
	@$(MAKE) install