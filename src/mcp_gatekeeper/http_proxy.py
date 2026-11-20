"""Async HTTP / Streamable-HTTP & SSE reverse proxy for Claude Web & remote MCP."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import urllib.parse
from typing import Any

from mcp_gatekeeper.broker import BrokerClient
from mcp_gatekeeper.protocol import (
    DEFAULT_GATED_METHODS,
    build_blocked_response,
    extract_intercepted_request,
    parse_jsonrpc_message,
)

logger = logging.getLogger("mcp_gatekeeper.http")

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class HttpMCPProxy:
    """Reverse proxy supporting MCP Streamable HTTP (POST/GET) and legacy SSE."""

    def __init__(
        self,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8787,
        upstream_url: str,
        server_name: str,
        broker_client: BrokerClient,
        gated_methods: frozenset[str] = DEFAULT_GATED_METHODS,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_url = upstream_url.rstrip("/")
        self.parsed_upstream = urllib.parse.urlparse(self.upstream_url)
        if self.parsed_upstream.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported upstream URL scheme: {upstream_url}")
        self.server_name = server_name
        self.broker_client = broker_client
        self.gated_methods = gated_methods
        self._server: asyncio.AbstractServer | None = None

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.listen_port

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.listen_host, self.listen_port
        )
        logger.info(
            "HTTP MCP proxy listening on http://%s:%d -> %s",
            self.listen_host,
            self.bound_port,
            self.upstream_url,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").strip().split(" ", 2)
            if len(parts) != 3:
                await self._send_simple(writer, 400, b"Bad Request")
                return
            method, path, _version = parts

            headers: dict[str, str] = {}
            while True:
                hline = await reader.readline()
                if not hline or hline in (b"\r\n", b"\n"):
                    break
                decoded = hline.decode("latin-1").rstrip("\r\n")
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            body = await self._read_body(reader, headers)

            # Inspect JSON-RPC payloads on POST requests
            if method.upper() == "POST" and body:
                blocked_resp = await self._inspect_and_gate(body)
                if blocked_resp is not None:
                    payload = json.dumps(blocked_resp).encode("utf-8")
                    await self._send_json(writer, 200, payload)
                    return

            await self._forward_upstream(method, path, headers, body, writer)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            logger.exception("HTTP proxy error: %s", exc)
            try:
                await self._send_simple(writer, 502, f"Bad Gateway: {exc}".encode())
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_body(
        self, reader: asyncio.StreamReader, headers: dict[str, str]
    ) -> bytes:
        te = headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            chunks: list[bytes] = []
            while True:
                size_line = await reader.readline()
                if not size_line:
                    break
                size_hex = size_line.decode("latin-1").strip().split(";", 1)[0]
                size = int(size_hex, 16)
                if size == 0:
                    await reader.readline()
                    break
                chunks.append(await reader.readexactly(size))
                await reader.readline()
            return b"".join(chunks)

        cl = headers.get("content-length")
        if cl:
            length = int(cl)
            if length > 0:
                return await reader.readexactly(length)
        return b""

    async def _inspect_and_gate(self, body: bytes) -> dict[str, Any] | list[Any] | None:
        parsed = parse_jsonrpc_message(body)
        if parsed is None:
            return None

        if isinstance(parsed, dict):
            intercepted = extract_intercepted_request(
                parsed,
                server_name=self.server_name,
                transport="http",
                gated_methods=self.gated_methods,
            )
            if intercepted is None:
                return None
            decision = await self.broker_client.request_approval(intercepted)
            if decision.allowed:
                return None
            return build_blocked_response(
                intercepted.jsonrpc_id,
                server_name=intercepted.server_name,
                tool_name=intercepted.tool_name,
                reason=decision.reason or "Request blocked by user via MCP Gatekeeper.",
            )

        if isinstance(parsed, list):
            # Evaluate all gated items in a JSON-RPC batch concurrently
            tasks = []
            for item in parsed:
                if isinstance(item, dict):
                    intercepted = extract_intercepted_request(
                        item,
                        server_name=self.server_name,
                        transport="http",
                        gated_methods=self.gated_methods,
                    )
                    if intercepted is not None:
                        tasks.append(
                            (intercepted, self.broker_client.request_approval(intercepted))
                        )
            if not tasks:
                return None
            decisions = await asyncio.gather(*(t[1] for t in tasks))
            blocked_items = [
                build_blocked_response(
                    intercepted.jsonrpc_id,
                    server_name=intercepted.server_name,
                    tool_name=intercepted.tool_name,
                    reason=dec.reason,
                )
                for (intercepted, _), dec in zip(tasks, decisions)
                if not dec.allowed
            ]
            if blocked_items:
                return blocked_items
        return None

    async def _forward_upstream(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        base_path = self.parsed_upstream.path.rstrip("/")
        target_path = f"{base_path}{path}" if path.startswith("/") else f"{base_path}/{path}"
        if not target_path:
            target_path = "/"

        fwd_headers = {
            k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }
        fwd_headers["host"] = self.parsed_upstream.netloc

        def _open_and_send() -> http.client.HTTPResponse:
            if self.parsed_upstream.scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    self.parsed_upstream.hostname or "localhost",
                    self.parsed_upstream.port or 443,
                    timeout=120,
                )
            else:
                conn = http.client.HTTPConnection(
                    self.parsed_upstream.hostname or "localhost",
                    self.parsed_upstream.port or 80,
                    timeout=120,
                )
            conn.request(method, target_path, body=body if body else None, headers=fwd_headers)
            return conn.getresponse()

        resp = await asyncio.to_thread(_open_and_send)
        resp_headers = [
            (k, v)
            for k, v in resp.getheaders()
            if k.lower() not in HOP_BY_HOP_HEADERS
        ]
        content_type = resp.getheader("Content-Type", "")

        if "text/event-stream" in content_type.lower():
            # Stream SSE events chunk by chunk
            header_lines = [f"HTTP/1.1 {resp.status} {resp.reason}\r\n"]
            for k, v in resp_headers:
                header_lines.append(f"{k}: {v}\r\n")
            header_lines.append("Connection: close\r\n\r\n")
            client_writer.write("".join(header_lines).encode("latin-1"))
            await client_writer.drain()

            while True:
                chunk = await asyncio.to_thread(resp.read1, 4096)
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()
        else:
            resp_body = await asyncio.to_thread(resp.read)
            header_lines = [f"HTTP/1.1 {resp.status} {resp.reason}\r\n"]
            for k, v in resp_headers:
                header_lines.append(f"{k}: {v}\r\n")
            header_lines.append(f"Content-Length: {len(resp_body)}\r\n")
            header_lines.append("Connection: close\r\n\r\n")
            client_writer.write("".join(header_lines).encode("latin-1") + resp_body)
            await client_writer.drain()

    async def _send_json(
        self, writer: asyncio.StreamWriter, status: int, payload: bytes
    ) -> None:
        head = (
            f"HTTP/1.1 {status} OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + payload)
        await writer.drain()

    async def _send_simple(
        self, writer: asyncio.StreamWriter, status: int, message: bytes
    ) -> None:
        head = (
            f"HTTP/1.1 {status} Error\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(message)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + message)
        await writer.drain()
