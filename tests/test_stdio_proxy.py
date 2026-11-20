"""End-to-end integration tests for StdioMCPProxy using mock_mcp_server.py."""

import asyncio
import json
import sys
import unittest
from pathlib import Path

from mcp_gatekeeper.broker import ApprovalBrokerServer, BrokerClient, PendingApproval
from mcp_gatekeeper.protocol import BLOCKED_ERROR_CODE
from mcp_gatekeeper.stdio_proxy import StdioMCPProxy

MOCK_SERVER = str(Path(__file__).resolve().parent.parent / "examples" / "mock_mcp_server.py")


class StdioProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_allow_block_and_concurrent_stdio_requests(self) -> None:
        pending: list[PendingApproval] = []
        broker = ApprovalBrokerServer(host="127.0.0.1", port=0, on_new_request=pending.append)
        await broker.start()

        # Connect a local TCP pair to simulate Claude Desktop stdin/stdout
        server_side_reader: asyncio.StreamReader | None = None
        server_side_writer: asyncio.StreamWriter | None = None
        connected = asyncio.Event()

        async def _accept(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            nonlocal server_side_reader, server_side_writer
            server_side_reader = r
            server_side_writer = w
            connected.set()

        pipe_srv = await asyncio.start_server(_accept, "127.0.0.1", 0)
        pipe_port = pipe_srv.sockets[0].getsockname()[1]

        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", pipe_port)
        await connected.wait()
        assert server_side_reader is not None and server_side_writer is not None

        proxy = StdioMCPProxy(
            [sys.executable, MOCK_SERVER, "--transport", "stdio"],
            server_name="demo-stdio",
            broker_client=BrokerClient(host="127.0.0.1", port=broker.bound_port),
        )
        proxy_task = asyncio.create_task(
            proxy.run(client_reader=server_side_reader, client_writer=server_side_writer)
        )

        try:
            # 1. tools/list passes through immediately without confirmation
            client_writer.write(
                (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode()
            )
            await client_writer.drain()
            line = await asyncio.wait_for(client_reader.readline(), timeout=3.0)
            resp = json.loads(line.decode())
            self.assertEqual(resp["id"], 1)
            self.assertIn("tools", resp["result"])

            # 2. Send two concurrent tools/call requests (id=2 allow, id=3 block)
            req_allow = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_note", "arguments": {"title": "hello"}}}
            req_block = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "delete_records", "arguments": {"table": "users"}}}
            client_writer.write((json.dumps(req_allow) + "\n" + json.dumps(req_block) + "\n").encode())
            await client_writer.drain()

            while len(pending) < 2:
                await asyncio.sleep(0.01)

            for item in pending:
                broker.resolve_from_thread(
                    item.approval_id,
                    allowed=(item.tool_name == "read_note"),
                )

            r_a = json.loads((await asyncio.wait_for(client_reader.readline(), timeout=3.0)).decode())
            r_b = json.loads((await asyncio.wait_for(client_reader.readline(), timeout=3.0)).decode())
            by_id = {r["id"]: r for r in (r_a, r_b)}

            self.assertIn("result", by_id[2])
            self.assertIn("Executed read_note", by_id[2]["result"]["content"][0]["text"])
            self.assertIn("error", by_id[3])
            self.assertEqual(by_id[3]["error"]["code"], BLOCKED_ERROR_CODE)
        finally:
            client_writer.close()
            await proxy_task
            pipe_srv.close()
            await pipe_srv.wait_closed()
            await broker.stop()


if __name__ == "__main__":
    unittest.main()
