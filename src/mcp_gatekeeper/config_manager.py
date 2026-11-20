"""Utility to safely wrap and unwrap Claude Desktop's claude_desktop_config.json."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def default_claude_config_path() -> Path:
    """Return the platform-specific default path for claude_desktop_config.json."""
    system = platform.system()
    if system == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if system == "Windows":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _is_wrapped(entry: dict[str, Any]) -> bool:
    args = entry.get("args") or []
    return (
        isinstance(args, list)
        and "mcp_gatekeeper.cli" in args
        or (
            isinstance(entry.get("command"), str)
            and "mcp-gatekeeper" in Path(entry["command"]).name
        )
    )


def wrap_claude_config(
    config_path: Path,
    *,
    broker_host: str = "127.0.0.1",
    broker_port: int = 8765,
    python_executable: str | None = None,
) -> tuple[Path, list[str]]:
    """Wrap all STDIO mcpServers in claude_desktop_config.json with MCP Gatekeeper.

    Returns (backup_path, list_of_wrapped_server_names).
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Claude Desktop config not found at {config_path}")

    raw_text = config_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError("Config file does not contain a valid 'mcpServers' object.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.with_suffix(f".json.bak.{timestamp}")
    shutil.copy2(config_path, backup_path)

    py_exec = python_executable or sys.executable
    wrapped_names: list[str] = []

    for name, entry in servers.items():
        if not isinstance(entry, dict) or "command" not in entry:
            continue
        if _is_wrapped(entry):
            continue

        orig_cmd = entry["command"]
        orig_args = entry.get("args") or []
        if not isinstance(orig_args, list):
            orig_args = [str(orig_args)]

        new_args = [
            "-m",
            "mcp_gatekeeper.cli",
            "stdio",
            "--server-name",
            name,
            "--broker-host",
            broker_host,
            "--broker-port",
            str(broker_port),
            "--",
            str(orig_cmd),
            *[str(a) for a in orig_args],
        ]
        entry["command"] = py_exec
        entry["args"] = new_args
        wrapped_names.append(name)

    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return backup_path, wrapped_names


def unwrap_claude_config(config_path: Path) -> list[str]:
    """Restore wrapped mcpServers in claude_desktop_config.json to their original commands."""
    if not config_path.exists():
        raise FileNotFoundError(f"Claude Desktop config not found at {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return []

    unwrapped: list[str] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict) or not _is_wrapped(entry):
            continue
        args = entry.get("args") or []
        if "--" in args:
            sep_idx = args.index("--")
            orig_parts = args[sep_idx + 1 :]
            if orig_parts:
                entry["command"] = orig_parts[0]
                entry["args"] = orig_parts[1:]
                unwrapped.append(name)

    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return unwrapped
