"""Central Approval Broker for coordinating concurrent MCP confirmation requests."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from mcp_gatekeeper.protocol import InterceptedRequest

logger = logging.getLogger("mcp_gatekeeper.broker")

DEFAULT_BROKER_HOST = "127.0.0.1"
DEFAULT_BROKER_PORT = 8765


@dataclass
class PendingApproval:
    """Represents a single MCP request awaiting user decision."""

    approval_id: str
    server_name: str
    transport: str
    method: str
    tool_name: str
    arguments: dict[str, Any]
    jsonrpc_id: Any
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PendingApproval:
        return cls(
            approval_id=str(payload["approval_id"]),
            server_name=str(payload.get("server_name", "unknown")),
            transport=str(payload.get("transport", "unknown")),
            method=str(payload.get("method", "tools/call")),
            tool_name=str(payload.get("tool_name", "<unknown>")),
            arguments=payload.get("arguments") or {},
            jsonrpc_id=payload.get("jsonrpc_id"),
            created_at=str(
                payload.get("created_at")
                or datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            ),
        )


@dataclass
class ApprovalDecision:
    """Decision returned by the broker to a waiting proxy."""

    allowed: bool
    reason: str = ""


class ApprovalBrokerServer:
    """Async TCP JSON-lines server that queues requests and resolves Futures on user action."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_BROKER_HOST,
        port: int = DEFAULT_BROKER_PORT,
        on_new_request: Callable[[PendingApproval], None] | None = None,
        on_request_cancelled: Callable[[str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._on_new_request = on_new_request
        self._on_request_cancelled = on_request_cancelled
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("Approval broker listening on %s:%d", self.host, self.bound_port)

    async def stop(self) -> None:
        # Fail-closed on all still-pending futures
        for approval_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(
                    ApprovalDecision(allowed=False, reason="Gatekeeper broker shutting down.")
                )
        self._pending.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def resolve_from_thread(
        self, approval_id: str, *, allowed: bool, reason: str = ""
    ) -> None:
        """Thread-safe resolution method called by the Tkinter UI thread."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(
            self._resolve_on_loop, approval_id, allowed, reason
        )

    def _resolve_on_loop(self, approval_id: str, allowed: bool, reason: str) -> None:
        fut = self._pending.pop(approval_id, None)
        if fut is not None and not fut.done():
            default_reason = (
                "Allowed by user."
                if allowed
                else "Request blocked by user via MCP Gatekeeper."
            )
            fut.set_result(
                ApprovalDecision(allowed=allowed, reason=reason or default_reason)
            )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        approval_id: str | None = None
        try:
            raw_line = await reader.readline()
            if not raw_line:
                return
            payload = json.loads(raw_line.decode("utf-8"))
            approval_id = str(payload.get("approval_id") or uuid.uuid4())
            payload["approval_id"] = approval_id
            pending = PendingApproval.from_payload(payload)

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[ApprovalDecision] = loop.create_future()
            self._pending[approval_id] = fut

            if self._on_new_request is not None:
                self._on_new_request(pending)

            decision = await fut
            response = json.dumps(
                {"allowed": decision.allowed, "reason": decision.reason}
            ).encode("utf-8") + b"\n"
            writer.write(response)
            await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            if approval_id and self._on_request_cancelled is not None:
                self._on_request_cancelled(approval_id)
        except Exception as exc:
            logger.exception("Broker client error: %s", exc)
            try:
                err = json.dumps(
                    {"allowed": False, "reason": f"Broker error: {exc}"}
                ).encode("utf-8") + b"\n"
                writer.write(err)
                await writer.drain()
            except Exception:
                pass
        finally:
            if approval_id:
                self._pending.pop(approval_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


class BrokerClient:
    """Async client used by STDIO and HTTP proxies to request user confirmation."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_BROKER_HOST,
        port: int = DEFAULT_BROKER_PORT,
        fail_open: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.fail_open = fail_open
        self.timeout_seconds = timeout_seconds

    async def request_approval(self, req: InterceptedRequest) -> ApprovalDecision:
        """Send an intercepted request to the central broker and await user decision."""
        approval_id = str(uuid.uuid4())
        payload = req.to_summary_dict()
        payload["approval_id"] = approval_id
        payload["created_at"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            writer.write(json.dumps(payload).encode("utf-8") + b"\n")
            await writer.drain()

            raw_resp = await asyncio.wait_for(
                reader.readline(), timeout=self.timeout_seconds
            )
            if not raw_resp:
                return ApprovalDecision(
                    allowed=self.fail_open,
                    reason="Gatekeeper broker closed connection before deciding.",
                )
            data = json.loads(raw_resp.decode("utf-8"))
            return ApprovalDecision(
                allowed=bool(data.get("allowed", False)),
                reason=str(data.get("reason", "")),
            )
        except asyncio.TimeoutError:
            return ApprovalDecision(
                allowed=False,
                reason="Confirmation timed out; request blocked for safety.",
            )
        except OSError as exc:
            msg = (
                f"Cannot reach MCP Gatekeeper broker at {self.host}:{self.port} ({exc})."
            )
            logger.warning("%s fail_open=%s", msg, self.fail_open)
            return ApprovalDecision(allowed=self.fail_open, reason=msg)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
