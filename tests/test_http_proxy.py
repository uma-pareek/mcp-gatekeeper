"""End-to-end integration tests for HttpMCPProxy using mock_mcp_server.py."""

import asyncio
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from examples.mock_mcp_server import _MCPHandler  # type: ignore
from mcp_gatekeeper.broker import ApprovalBrokerServer, BrokerClient, PendingApproval
from mcp_gatekeeper.http_proxy import HttpMCPProxy
from mcp_gatekeeper.protocol import BLOCKED_ERROR_CODE


class HttpProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_allow_block_and_passthrough(self) -> None:
        # Start upstream mock HTTP MCP server
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MCPHandler)
        upstream_port = upstream.server_address[1]
        up_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        up_thread.start()

        pending: list[PendingApproval] = []
        broker = ApprovalBrokerServer(host="127.0.0.1", port=0, on_new_request=pending.append)
        await broker.start()

        proxy = HttpMCPProxy(
            listen_host="127.0.0.1",
            listen_port=0,
            upstream_url=f"http://127.0.0.1:{upstream_port}",
            server_name="demo-web",
            broker_client=BrokerClient(host="127.0.0.1", port=broker.bound_port),
        )
        await proxy.start()
        proxy_url = f"http://127.0.0.1:{proxy.bound_port}/mcp"

        def _post(payload: dict) -> dict:
            req = urllib.request.Request(
                proxy_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            # 1. tools/list passthrough
            list_resp = await asyncio.to_thread(
                _post, {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}
            )
            self.assertIn("tools", list_resp["result"])

            # 2. Concurrent tools/call: one allowed, one blocked
            t_allow = asyncio.create_task(
                asyncio.to_thread(
                    _post,
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {"name": "read_note", "arguments": {"title": "spec"}},
                    },
                )
            )
            t_block = asyncio.create_task(
                asyncio.to_thread(
                    _post,
                    {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "tools/call",
                        "params": {"name": "delete_records", "arguments": {"table": "orders"}},
                    },
                )
            )

            while len(pending) < 2:
                await asyncio.sleep(0.01)

            for item in pending:
                broker.resolve_from_thread(
                    item.approval_id,
                    allowed=(item.tool_name == "read_note"),
                )

            res_allow, res_block = await asyncio.gather(t_allow, t_block)
            self.assertIn("Executed read_note", res_allow["result"]["content"][0]["text"])
            self.assertEqual(res_block["error"]["code"], BLOCKED_ERROR_CODE)
        finally:
            await proxy.stop()
            await broker.stop()
            upstream.shutdown()
            upstream.server_close()


if __name__ == "__main__":
    unittest.main()
