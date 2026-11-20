"""Unit tests for JSON-RPC 2.0 / MCP protocol parsing and denial responses."""

import unittest

from mcp_gatekeeper.protocol import (
    BLOCKED_ERROR_CODE,
    STRICT_GATED_METHODS,
    build_blocked_response,
    extract_intercepted_request,
    parse_jsonrpc_message,
)


class ProtocolTests(unittest.TestCase):
    def test_parse_valid_and_invalid_json(self) -> None:
        self.assertEqual(parse_jsonrpc_message('{"jsonrpc":"2.0","id":1}'), {"jsonrpc": "2.0", "id": 1})
        self.assertIsNone(parse_jsonrpc_message("not-json"))
        self.assertIsNone(parse_jsonrpc_message("   "))

    def test_passthrough_non_gated_methods_and_notifications(self) -> None:
        init_msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        list_msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        notif_msg = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "x"}}

        self.assertIsNone(extract_intercepted_request(init_msg, server_name="fs", transport="stdio"))
        self.assertIsNone(extract_intercepted_request(list_msg, server_name="fs", transport="stdio"))
        self.assertIsNone(extract_intercepted_request(notif_msg, server_name="fs", transport="stdio"))

    def test_extract_tools_call(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "write_file", "arguments": {"path": "/tmp/a.txt", "content": "hi"}},
        }
        req = extract_intercepted_request(msg, server_name="filesystem", transport="stdio")
        self.assertIsNotNone(req)
        assert req is not None
        self.assertEqual(req.jsonrpc_id, 42)
        self.assertEqual(req.server_name, "filesystem")
        self.assertEqual(req.transport, "stdio")
        self.assertEqual(req.tool_name, "write_file")
        self.assertEqual(req.arguments["path"], "/tmp/a.txt")

    def test_strict_mode_gates_resources_read(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": "r-1",
            "method": "resources/read",
            "params": {"uri": "file:///etc/hosts"},
        }
        self.assertIsNone(extract_intercepted_request(msg, server_name="fs", transport="http"))
        req = extract_intercepted_request(
            msg,
            server_name="fs",
            transport="http",
            gated_methods=STRICT_GATED_METHODS,
        )
        self.assertIsNotNone(req)
        assert req is not None
        self.assertEqual(req.tool_name, "resource:file:///etc/hosts")

    def test_build_blocked_response(self) -> None:
        resp = build_blocked_response(99, server_name="db", tool_name="drop_table")
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 99)
        self.assertEqual(resp["error"]["code"], BLOCKED_ERROR_CODE)
        self.assertEqual(resp["error"]["data"]["tool"], "drop_table")


if __name__ == "__main__":
    unittest.main()
