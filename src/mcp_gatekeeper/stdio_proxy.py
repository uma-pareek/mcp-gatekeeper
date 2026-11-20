"""Bidirectional async STDIO proxy for intercepting Claude Desktop MCP traffic."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import threading
from typing import Sequence

from mcp_gatekeeper.broker import BrokerClient
from mcp_gatekeeper.protocol import (
    DEFAULT_GATED_METHODS,
    InterceptedRequest,
    build_blocked_response,
    extract_intercepted_request,
    parse_jsonrpc_message,
)

logger = logging.getLogger("mcp_gatekeeper.stdio")


class StdioMCPProxy:
    """Wraps a local STDIO MCP server process and gates configured JSON-RPC methods."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        server_name: str,
        broker_client: BrokerClient,
        gated_methods: frozenset[str] = DEFAULT_GATED_METHODS,
    ) -> None:
        if not command:
            raise ValueError("Target MCP server command cannot be empty.")
        self.command = list(command)
        self.server_name = server_name
        self.broker_client = broker_client
        self.gated_methods = gated_methods
        self._stdin_lock = asyncio.Lock()
        self._stdout_lock = asyncio.Lock()
        self._pending_tasks: set[asyncio.Task[None]] = set()
        self._process: asyncio.subprocess.Process | None = None

    async def run(
        self,
        client_reader: asyncio.StreamReader | None = None,
        client_writer: asyncio.StreamWriter | None = None,
        err_writer: asyncio.StreamWriter | None = None,
    ) -> int:
        """Start the wrapped MCP server and proxy traffic until EOF or termination."""
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None

        if client_reader is None:
            client_reader = await self._connect_stdin_reader()

        tasks = [
            asyncio.create_task(
                self._pump_client_to_server(client_reader, client_writer)
            ),
            asyncio.create_task(
                self._pump_server_stdout(self._process.stdout, client_writer)
            ),
            asyncio.create_task(
                self._pump_server_stderr(self._process.stderr, err_writer)
            ),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                logger.debug("Proxy pump ended with exception: %s", exc)

        # Cancel any still-waiting approval tasks cleanly
        for ptask in list(self._pending_tasks):
            ptask.cancel()

        return await self._terminate_child()

    async def _connect_stdin_reader(self) -> asyncio.StreamReader:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()

        # ProactorEventLoop's connect_read_pipe can fail for the anonymous stdin
        # pipe provided by Claude Desktop on Windows (WinError 6). Read that pipe
        # on a daemon thread and feed complete JSON-RPC lines back to the loop.
        if sys.platform == "win32":
            def _read_stdin() -> None:
                try:
                    while True:
                        raw_line = sys.stdin.buffer.readline()
                        if not raw_line:
                            loop.call_soon_threadsafe(reader.feed_eof)
                            return
                        loop.call_soon_threadsafe(reader.feed_data, raw_line)
                except (OSError, ValueError, RuntimeError) as exc:
                    logger.debug("Claude stdin pipe closed: %s", exc)
                    try:
                        loop.call_soon_threadsafe(reader.feed_eof)
                    except RuntimeError:
                        pass

            threading.Thread(
                target=_read_stdin,
                name="mcp-gatekeeper-stdin-reader",
                daemon=True,
            ).start()
            return reader

        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        return reader

    async def _write_to_child(self, line: bytes) -> None:
        if self._process is None or self._process.stdin is None:
            return
        async with self._stdin_lock:
            if not self._process.stdin.is_closing():
                self._process.stdin.write(line)
                await self._process.stdin.drain()

    async def _write_to_client(
        self, line: bytes, client_writer: asyncio.StreamWriter | None
    ) -> None:
        async with self._stdout_lock:
            if client_writer is not None:
                client_writer.write(line)
                await client_writer.drain()
            else:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()

    async def _pump_client_to_server(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter | None,
    ) -> None:
        while True:
            raw_line = await client_reader.readline()
            if not raw_line:
                if self._process and self._process.stdin:
                    self._process.stdin.close()
                break

            parsed = parse_jsonrpc_message(raw_line)
            if isinstance(parsed, dict):
                intercepted = extract_intercepted_request(
                    parsed,
                    server_name=self.server_name,
                    transport="stdio",
                    gated_methods=self.gated_methods,
                )
                if intercepted is not None:
                    task = asyncio.create_task(
                        self._handle_gated_request(
                            intercepted, raw_line, client_writer
                        )
                    )
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
                    continue

            await self._write_to_child(raw_line)

    async def _handle_gated_request(
        self,
        intercepted: InterceptedRequest,
        raw_line: bytes,
        client_writer: asyncio.StreamWriter | None,
    ) -> None:
        decision = await self.broker_client.request_approval(intercepted)
        if decision.allowed:
            await self._write_to_child(raw_line)
        else:
            err_payload = build_blocked_response(
                intercepted.jsonrpc_id,
                server_name=intercepted.server_name,
                tool_name=intercepted.tool_name,
                reason=decision.reason or "Request blocked by user via MCP Gatekeeper.",
            )
            err_bytes = json.dumps(err_payload).encode("utf-8") + b"\n"
            await self._write_to_client(err_bytes, client_writer)

    async def _pump_server_stdout(
        self,
        server_stdout: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter | None,
    ) -> None:
        while True:
            raw_line = await server_stdout.readline()
            if not raw_line:
                break
            await self._write_to_client(raw_line, client_writer)

    async def _pump_server_stderr(
        self,
        server_stderr: asyncio.StreamReader,
        err_writer: asyncio.StreamWriter | None,
    ) -> None:
        while True:
            raw_line = await server_stderr.readline()
            if not raw_line:
                break
            if err_writer is not None:
                err_writer.write(raw_line)
                await err_writer.drain()
            else:
                sys.stderr.buffer.write(raw_line)
                sys.stderr.buffer.flush()

    async def _terminate_child(self) -> int:
        if self._process is None:
            return 0
        if self._process.returncode is not None:
            return self._process.returncode
        try:
            self._process.terminate()
            return await asyncio.wait_for(self._process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self._process.kill()
            return await self._process.wait()
        except ProcessLookupError:
            return 0


def run_stdio_proxy(
    command: Sequence[str],
    *,
    server_name: str,
    broker_host: str,
    broker_port: int,
    fail_open: bool = False,
    gated_methods: frozenset[str] = DEFAULT_GATED_METHODS,
) -> int:
    """Entry point for running the STDIO proxy synchronously."""
    client = BrokerClient(host=broker_host, port=broker_port, fail_open=fail_open)
    proxy = StdioMCPProxy(
        command,
        server_name=server_name,
        broker_client=client,
        gated_methods=gated_methods,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal() -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    try:
        return loop.run_until_complete(proxy.run())
    except asyncio.CancelledError:
        return 0
    finally:
        loop.close()
