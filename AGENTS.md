# AGENTS.md — Yami Learning Companion

This file tells AI coding agents (Antigravity, Codex, WorkBuddy, Claude Code, Cursor, etc.) how to deploy and run this repository with minimal friction.

## One-Command Local Demo (Windows / Linux / macOS, Zero Docker)

When the user asks to deploy, preview, or run a local demo of this repository:

```bash
python yami.py demo
```

This single cross-platform command (pure Python stdlib, Python 3.9+):
1. Creates `.venv` and installs lightweight dependencies (SQLite + in-memory Graphiti, no Docker required).
2. Copies `.env.example` to `.env` if missing.
3. Starts the FastAPI Guardian Web & API on `http://127.0.0.1:8000` (and `0.0.0.0:8000` for LAN devices).
4. Seeds demo family data and prints the clickable Guardian Web URL: `http://127.0.0.1:8000/guardian`.

## Daily Use (With Real LLM / DingTalk Keys)

1. Edit `.env` with `YAMI_ARK_API_KEY` (Doubao Seed 2.1) and/or `YAMI_DEEPSEEK_API_KEY`.
2. Run `python yami.py start` (Windows/Linux/macOS) or `./scripts/yami_up.sh start` (macOS/Linux background daemons).

## Tests

```bash
python yami.py test
```

## ESP-Mosaico S31 Firmware Flashing

```bash
./scripts/flash_mosaico.sh
```
