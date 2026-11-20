# MCP Gatekeeper

**MCP Gatekeeper** is a lightweight, zero-dependency Python security proxy and human-in-the-loop confirmation firewall for **Model Context Protocol (MCP)** requests initiated by **Claude Desktop** and **Claude Web**.

Whenever Claude attempts to invoke an MCP tool (`tools/call`), MCP Gatekeeper intercepts the request, holds it pending, and displays an **always-on-top confirmation popup** showing the target MCP server, transport (`STDIO` or `HTTP`), tool name, and full JSON parameters. You can click **Allow** to release the request for execution or **Block** to deny it cleanly with a JSON-RPC 2.0 error response.

---

## Features

- **Dual-Transport Interception**:
  - **Claude Desktop (Local STDIO)**: Transparent bidirectional `asyncio` STDIO proxy + automated `claude_desktop_config.json` wrapper (`wrap-config` / `unwrap-config`) with automatic timestamped backups.
  - **Claude Web & Remote Connectors (Streamable HTTP / SSE)**: Async HTTP/HTTPS reverse proxy that inspects `POST` JSON-RPC payloads (single or batched) and streams `text/event-stream` SSE responses.
- **Always-On-Top Confirmation Modal (`tkinter`)**:
  - Pops up above all active windows (`-topmost`).
  - Queues multiple simultaneous requests (`Pending 1 of N`) with `â—€ Prev` / `Next â–¶` navigation and keyboard shortcuts (`Enter` to Allow, `Esc` to Block).
- **Concurrent & Fail-Closed**:
  - Uses a central TCP JSON-lines Approval Broker (`127.0.0.1:8765`) so multiple STDIO server processes and HTTP connections never collide.
  - Fails closed (`-32001: Request blocked by user via MCP Gatekeeper`) if the broker is unreachable or the popup is closed.
- **Zero External Dependencies**: Built 100% on the Python 3.10+ Standard Library.

---

## Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/uma-pareek/mcp-gatekeeper.git
   cd mcp-gatekeeper
   ```
2. **Verify Python 3.10+ and Tkinter**:
   - **macOS / Windows**: `tkinter` is included with standard Python.org installers.
   - **Ubuntu / Debian Linux**:
     ```bash
     sudo apt-get update && sudo apt-get install -y python3-tk
     ```
3. **Install in editable mode**:
   ```bash
   pip install -e .
   ```

---

## Quick Start

### 1. Start the Gatekeeper Daemon (Background Broker + Popup UI)

Keep this running in a terminal (or as a background service):

```bash
mcp-gatekeeper daemon
```

### 2. Configure Claude Desktop (Local STDIO Servers)

Automatically wrap all local `mcpServers` in your `claude_desktop_config.json` (a timestamped `.bak` backup is created automatically):

```bash
mcp-gatekeeper wrap-config
```

Restart **Claude Desktop**. Every `tools/call` to any local MCP server will now trigger the always-on-top confirmation popup.

To restore your original `claude_desktop_config.json` at any time:

```bash
mcp-gatekeeper unwrap-config
```

### 3. Configure Claude Web (Custom Connectors / Remote MCP)

Claude Web (`claude.ai`) connects to remote MCP servers via **Settings â†’ Connectors â†’ Add custom connector** over Streamable HTTP or SSE. Because Anthropic's cloud backend initiates the connection to your connector URL, route your connector through MCP Gatekeeper's HTTP reverse proxy:

1. Start the HTTP reverse proxy pointing at your upstream MCP server:
   ```bash
   mcp-gatekeeper http \
     --server-name my-remote-mcp \
     --listen-host 127.0.0.1 \
     --listen-port 8787 \
     --upstream https://your-mcp-server.example.com/mcp
   ```
2. Expose `http://127.0.0.1:8787` to Claude Web using a secure tunnel (e.g. `cloudflared tunnel --url http://127.0.0.1:8787` or `ngrok http 8787`).
3. Paste the public `https://.../mcp` tunnel URL into **Claude Web â†’ Settings â†’ Connectors â†’ Add custom connector**.

---

## Running the Test Suite

Run all unit and end-to-end concurrency integration tests (no external packages required):

```bash
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

---

## Recording the Demonstration Video

A standalone mock MCP server is included in `examples/mock_mcp_server.py` so you can easily demonstrate both **Allow** and **Block** flows:

1. Terminal 1 â€” Start the daemon:
   ```bash
   mcp-gatekeeper daemon
   ```
2. Terminal 2 â€” Start the mock HTTP MCP server and proxy (or wrap `examples/claude_desktop_config.example.json` for Claude Desktop):
   ```bash
   python3 examples/mock_mcp_server.py --transport http --port 9000
   mcp-gatekeeper http --server-name demo-server --listen-port 8787 --upstream http://127.0.0.1:9000
   ```
3. Ask Claude to call `read_note` (click **Allow** in the popup and show the tool result) and then `delete_records` (click **Block** in the popup and show the `-32001` denial returned to Claude).

