#!/usr/bin/env python3
"""Cross-platform ultra-minimal launcher for Yami Learning Companion (Windows / Linux / macOS).

Designed for effortless sharing and one-command local setup via any AI coding agent
(Antigravity, Codex, WorkBuddy, Claude Code, Cursor, etc.):
  - Zero Docker required (uses local SQLite + in-memory Graphiti out of the box)
  - Zero Bash required (pure Python stdlib, works in Windows PowerShell/CMD, Linux, macOS)

Usage:
    python yami.py demo    # Auto-setup + seed demo data + start Guardian Web on :8000
    python yami.py setup   # Create .venv, install lightweight dependencies, copy .env.example
    python yami.py start   # Start API + Worker (+ DingTalk Stream / dws poller if configured)
    python yami.py test    # Run pytest suite
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
IS_WIN = platform.system() == "Windows"


def venv_bin(name: str) -> Path:
    if IS_WIN:
        return VENV_DIR / "Scripts" / f"{name}.exe"
    return VENV_DIR / "bin" / name


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def cmd_setup(prod: bool = False) -> None:
    print(f"==> [Yami Setup] OS={platform.system()} Python={sys.version.split()[0]}")
    if not VENV_DIR.exists():
        print("==> Creating virtual environment (.venv)...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])

    py = str(venv_bin("python"))
    extra = ".[dev,prod]" if prod else ".[dev]"
    print(f"==> Installing packages ({extra})...")
    subprocess.check_call([py, "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([py, "-m", "pip", "install", "-e", extra], cwd=str(ROOT))

    env_file = ROOT / ".env"
    example_file = ROOT / ".env.example"
    if not env_file.exists() and example_file.exists():
        shutil.copyfile(example_file, env_file)
        print("==> Created .env from .env.example (edit it to add Doubao Seed / DeepSeek keys).")

    print("\n✅ Setup complete! Run: python yami.py start  (or: python yami.py demo)")


def _wait_api_ready(url: str, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def cmd_start(port: int = 8000, seed_demo: bool = False, with_dingtalk: bool = True) -> None:
    py = venv_bin("python")
    if not py.exists():
        print("==> .venv not found, running first-time setup automatically...")
        cmd_setup(prod=False)

    py_str = str(py)
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = f"{local_bin}{os.pathsep}{env.get('PATH', '')}"

    procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(label: str, args: list[str]) -> None:
        print(f"==> Starting {label}...")
        p = subprocess.Popen(args, cwd=str(ROOT), env=env)
        procs.append((label, p))

    # 1. Core API + Guardian Web
    spawn(
        "yami-api",
        [py_str, "-m", "uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", str(port)],
    )
    # 2. Background Worker (Reminders + Graphiti Outbox)
    spawn("yami-worker", [py_str, "-m", "backend.worker"])

    # 3. Optional DingTalk Stream & dws Poller (only if configured / installed)
    if with_dingtalk:
        has_dt_env = bool(env.get("DINGTALK_CLIENT_ID") and env.get("DINGTALK_CLIENT_SECRET"))
        if has_dt_env:
            spawn("yami-dingtalk-stream", [py_str, "-m", "backend.adapters.dingtalk_stream"])
        if shutil.which("dws", path=env["PATH"]):
            spawn("yami-dws-poller", [py_str, "-m", "backend.adapters.dws_poller", "--interval", "120"])

    if _wait_api_ready(f"http://127.0.0.1:{port}/health"):
        if seed_demo:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/guardian/dev/seed-demo",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    print("==> Seeded demo family & assignments.")
            except Exception as exc:
                print(f"==> Demo seed skipped: {exc}")

        lan_ip = get_lan_ip()
        print("\n" + "=" * 64)
        print(f"  Yami Learning Companion is running! ({platform.system()})")
        print(f"  Guardian Web (Local):  http://127.0.0.1:{port}/guardian")
        print(f"  Guardian Web (LAN):    http://{lan_ip}:{port}/guardian")
        print(f"  Mosaico Backend URL:   http://{lan_ip}:{port}")
        print("  Press Ctrl+C to stop all services cleanly.")
        print("=" * 64 + "\n")

    def shutdown(*_args) -> None:
        print("\n==> Stopping all Yami services...")
        for label, p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=4)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            time.sleep(1.0)
            # If the core API exits unexpectedly, stop everything
            if procs and procs[0][1].poll() is not None:
                print(f"==> {procs[0][0]} exited with code {procs[0][1].returncode}")
                shutdown()
    except KeyboardInterrupt:
        shutdown()


def cmd_test() -> None:
    py = venv_bin("python")
    if not py.exists():
        cmd_setup(prod=False)
    raise SystemExit(subprocess.call([str(py), "-m", "pytest", "-q"], cwd=str(ROOT)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Yami Learning Companion cross-platform launcher")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="Create .venv and install dependencies")
    p_setup.add_argument("--prod", action="store_true", help="Also install PostgreSQL & Neo4j drivers")

    p_start = sub.add_parser("start", help="Start Yami services (API + Worker + optional DingTalk)")
    p_start.add_argument("--port", type=int, default=8000)
    p_start.add_argument("--no-dingtalk", action="store_true")

    p_demo = sub.add_parser("demo", help="Auto-setup, seed demo data, and start immediately")
    p_demo.add_argument("--port", type=int, default=8000)

    sub.add_parser("test", help="Run pytest suite")

    args = parser.parse_args()
    if args.command == "setup":
        cmd_setup(prod=args.prod)
    elif args.command == "start":
        cmd_start(port=args.port, seed_demo=False, with_dingtalk=not args.no_dingtalk)
    elif args.command == "demo":
        cmd_start(port=args.port, seed_demo=True, with_dingtalk=False)
    elif args.command == "test":
        cmd_test()


if __name__ == "__main__":
    main()
