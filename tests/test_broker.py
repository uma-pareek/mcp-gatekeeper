"""Concurrency and fail-closed tests for the central ApprovalBrokerServer & BrokerClient."""

import asyncio
import unittest

from mcp_gatekeeper.broker import ApprovalBrokerServer, BrokerClient, PendingApproval
from mcp_gatekeeper.protocol import InterceptedRequest


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_resolved_out_of_order(self) -> None:
        received: list[PendingApproval] = []
        broker = ApprovalBrokerServer(
            host="127.0.0.1",
            port=0,
            on_new_request=received.append,
        )
        await broker.start()
        try:
            client = BrokerClient(host="127.0.0.1", port=broker.bound_port)
            req1 = InterceptedRequest(
                jsonrpc_id=1,
                method="tools/call",
                server_name="srv-a",
                transport="stdio",
                tool_name="tool_one",
                arguments={"x": 1},
            )
            req2 = InterceptedRequest(
                jsonrpc_id=2,
                method="tools/call",
                server_name="srv-b",
                transport="http",
                tool_name="tool_two",
                arguments={"y": 2},
            )

            t1 = asyncio.create_task(client.request_approval(req1))
            t2 = asyncio.create_task(client.request_approval(req2))

            while len(received) < 2:
                await asyncio.sleep(0.01)

            # Resolve second request first (Allow), then first request (Block)
            by_tool = {r.tool_name: r for r in received}
            broker.resolve_from_thread(by_tool["tool_two"].approval_id, allowed=True)
            broker.resolve_from_thread(by_tool["tool_one"].approval_id, allowed=False)

            d1, d2 = await asyncio.gather(t1, t2)
            self.assertFalse(d1.allowed)
            self.assertTrue(d2.allowed)
        finally:
            await broker.stop()

    async def test_fail_closed_when_broker_unreachable(self) -> None:
        client = BrokerClient(host="127.0.0.1", port=59999, fail_open=False)
        req = InterceptedRequest(
            jsonrpc_id=1,
            method="tools/call",
            server_name="srv",
            transport="stdio",
            tool_name="danger",
        )
        decision = await client.request_approval(req)
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
