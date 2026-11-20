"""JSON-RPC 2.0 and Model Context Protocol (MCP) message inspection helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Default MCP JSON-RPC methods that execute actions or read sensitive context
DEFAULT_GATED_METHODS: frozenset[str] = frozenset(
    {
        "tools/call",
    }
)

# Extended methods if the user enables strict mode
STRICT_GATED_METHODS: frozenset[str] = frozenset(
    {
        "tools/call",
        "resources/read",
        "prompts/get",
    }
)

BLOCKED_ERROR_CODE = -32001


@dataclass(frozen=True)
class InterceptedRequest:
    """Structured metadata extracted from an intercepted MCP JSON-RPC request."""

    jsonrpc_id: Any
    method: str
    server_name: str
    transport: str  # "stdio" or "http"
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_message: dict[str, Any] = field(default_factory=dict)

    def to_summary_dict(self) -> dict[str, Any]:
        """Serialize into a compact dictionary for IPC with the Approval Broker."""
        return {
            "jsonrpc_id": self.jsonrpc_id,
            "method": self.method,
            "server_name": self.server_name,
            "transport": self.transport,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
        }


def parse_jsonrpc_message(raw_line: str | bytes) -> dict[str, Any] | list[Any] | None:
    """Safely parse a JSON-RPC 2.0 message line; returns None if invalid JSON."""
    if isinstance(raw_line, bytes):
        text = raw_line.decode("utf-8", errors="replace").strip()
    else:
        text = raw_line.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def extract_intercepted_request(
    message: dict[str, Any],
    *,
    server_name: str,
    transport: str,
    gated_methods: frozenset[str] = DEFAULT_GATED_METHODS,
) -> InterceptedRequest | None:
    """Return an InterceptedRequest if `message` is a gated MCP request, else None.

    Notifications (requests without an `id`) and responses (`result`/`error` without
    `method`) are never gated and return None.
    """
    if not isinstance(message, dict):
        return None

    method = message.get("method")
    if not isinstance(method, str) or method not in gated_methods:
        return None

    # JSON-RPC 2.0 requests that expect a response have an "id" key (can be int or str)
    if "id" not in message or message["id"] is None:
        return None

    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    if method == "tools/call":
        tool_name = str(params.get("name", "<unknown_tool>"))
        raw_args = params.get("arguments", {})
        arguments = raw_args if isinstance(raw_args, dict) else {"value": raw_args}
    elif method == "resources/read":
        tool_name = f"resource:{params.get('uri', '<unknown_uri>')}"
        arguments = params
    elif method == "prompts/get":
        tool_name = f"prompt:{params.get('name', '<unknown_prompt>')}"
        arguments = params
    else:
        tool_name = method
        arguments = params

    return InterceptedRequest(
        jsonrpc_id=message["id"],
        method=method,
        server_name=server_name,
        transport=transport,
        tool_name=tool_name,
        arguments=arguments,
        raw_message=message,
    )


def build_blocked_response(
    jsonrpc_id: Any,
    *,
    server_name: str,
    tool_name: str,
    reason: str = "Request blocked by user via MCP Gatekeeper.",
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response indicating user denial."""
    return {
        "jsonrpc": "2.0",
        "id": jsonrpc_id,
        "error": {
            "code": BLOCKED_ERROR_CODE,
            "message": reason,
            "data": {
                "blocked_by": "mcp-gatekeeper",
                "server": server_name,
                "tool": tool_name,
            },
        },
    }
