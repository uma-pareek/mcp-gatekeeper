"""Unified CLI entrypoint for MCP Gatekeeper."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path

from mcp_gatekeeper.broker import (
    DEFAULT_BROKER_HOST,
    DEFAULT_BROKER_PORT,
    ApprovalBrokerServer,
    PendingApproval,
)
from mcp_gatekeeper.config_manager import (
    default_claude_config_path,
    unwrap_claude_config,
    wrap_claude_config,
)
from mcp_gatekeeper.http_proxy import HttpMCPProxy
from mcp_gatekeeper.protocol import DEFAULT_GATED_METHODS, STRICT_GATED_METHODS
from mcp_gatekeeper.stdio_proxy import run_stdio_proxy

logger = logging.getLogger("mcp_gatekeeper")


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Run the central Approval Broker + GUI (or headless auto-responder)."""
    loop = asyncio.new_event_loop()

    if args.headless_auto:
        allowed = args.headless_auto == "allow"

        async def _run_headless() -> None:
            broker: ApprovalBrokerServer | None = None

            def _on_req(req: PendingApproval) -> None:
                logger.info(
                    "[HEADLESS %s] server=%s transport=%s tool=%s args=%s",
                    args.headless_auto.upper(),
                    req.server_name,
                    req.transport,
                    req.tool_name,
                    req.arguments,
                )
                assert broker is not None
                broker.resolve_from_thread(req.approval_id, allowed=allowed)

            broker = ApprovalBrokerServer(
                host=args.host,
                port=args.port,
                on_new_request=_on_req,
            )
            await broker.start()
            stop_event = asyncio.Event()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except NotImplementedError:
                    pass

            await stop_event.wait()
            await broker.stop()

        try:
            loop.run_until_complete(_run_headless())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()
        return 0

    # Desktop GUI mode (Tkinter on main thread, asyncio broker in daemon thread)
    try:
        from mcp_gatekeeper.ui import GatekeeperTkUI
    except ImportError as exc:
        logger.error("Tkinter is not available (%s). Use --headless-auto for CLI mode.", exc)
        return 1

    broker_holder: dict[str, ApprovalBrokerServer] = {}
    ready_event = threading.Event()

    def _on_decision(approval_id: str, allowed: bool) -> None:
        broker = broker_holder.get("broker")
        if broker is not None:
            broker.resolve_from_thread(approval_id, allowed=allowed)

    ui = GatekeeperTkUI(on_decision=_on_decision)

    def _broker_thread() -> None:
        asyncio.set_event_loop(loop)
        broker = ApprovalBrokerServer(
            host=args.host,
            port=args.port,
            on_new_request=ui.enqueue_request,
            on_request_cancelled=ui.cancel_request,
        )
        broker_holder["broker"] = broker

        async def _serve() -> None:
            await broker.start()
            ready_event.set()
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await broker.stop()

        try:
            loop.run_until_complete(_serve())
        finally:
            loop.close()

    t = threading.Thread(target=_broker_thread, daemon=True)
    t.start()
    ready_event.wait(timeout=5.0)
    logger.info(
        "MCP Gatekeeper daemon running on %s:%d. Waiting for MCP requests...",
        args.host,
        args.port,
    )

    try:
        ui.run_mainloop()
    finally:
        if not loop.is_closed():
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
        t.join(timeout=2.0)
    return 0


def _cmd_stdio(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        logger.error("No target MCP command provided after '--'.")
        return 2

    gated = STRICT_GATED_METHODS if args.strict else DEFAULT_GATED_METHODS
    return run_stdio_proxy(
        command,
        server_name=args.server_name,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        fail_open=args.fail_open,
        gated_methods=gated,
    )


def _cmd_http(args: argparse.Namespace) -> int:
    from mcp_gatekeeper.broker import BrokerClient

    gated = STRICT_GATED_METHODS if args.strict else DEFAULT_GATED_METHODS
    client = BrokerClient(
        host=args.broker_host,
        port=args.broker_port,
        fail_open=args.fail_open,
    )
    proxy = HttpMCPProxy(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        upstream_url=args.upstream,
        server_name=args.server_name,
        broker_client=client,
        gated_methods=gated,
    )
    try:
        asyncio.run(proxy.serve_forever())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_wrap_config(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else default_claude_config_path()
    backup, wrapped = wrap_claude_config(
        path,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
    )
    print(f"Backup saved to: {backup}")
    print(f"Wrapped MCP servers ({len(wrapped)}): {', '.join(wrapped) or 'none'}")
    print("Restart Claude Desktop for changes to take effect.")
    return 0


def _cmd_unwrap_config(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else default_claude_config_path()
    unwrapped = unwrap_claude_config(path)
    print(f"Restored MCP servers ({len(unwrapped)}): {', '.join(unwrapped) or 'none'}")
    print("Restart Claude Desktop for changes to take effect.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-gatekeeper",
        description="Monitor and gate MCP requests from Claude Desktop and Claude Web.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # daemon
    p_daemon = sub.add_parser("daemon", help="Run central Approval Broker & confirmation UI")
    p_daemon.add_argument("--host", default=DEFAULT_BROKER_HOST)
    p_daemon.add_argument("--port", type=int, default=DEFAULT_BROKER_PORT)
    p_daemon.add_argument(
        "--headless-auto",
        choices=["allow", "block"],
        default=None,
        help="Run without GUI and auto-allow or auto-block (for testing/CI)",
    )
    p_daemon.set_defaults(func=_cmd_daemon)

    # stdio
    p_stdio = sub.add_parser("stdio", help="Wrap a local STDIO MCP server (Claude Desktop)")
    p_stdio.add_argument("--server-name", required=True)
    p_stdio.add_argument("--broker-host", default=DEFAULT_BROKER_HOST)
    p_stdio.add_argument("--broker-port", type=int, default=DEFAULT_BROKER_PORT)
    p_stdio.add_argument("--strict", action="store_true", help="Also gate resources/read and prompts/get")
    p_stdio.add_argument("--fail-open", action="store_true", help="Allow requests if daemon is unreachable")
    p_stdio.add_argument("command", nargs=argparse.REMAINDER)
    p_stdio.set_defaults(func=_cmd_stdio)

    # http
    p_http = sub.add_parser("http", help="Run HTTP/SSE reverse proxy (Claude Web / remote MCP)")
    p_http.add_argument("--upstream", required=True, help="Upstream MCP server URL")
    p_http.add_argument("--server-name", default="remote-mcp")
    p_http.add_argument("--listen-host", default="127.0.0.1")
    p_http.add_argument("--listen-port", type=int, default=8787)
    p_http.add_argument("--broker-host", default=DEFAULT_BROKER_HOST)
    p_http.add_argument("--broker-port", type=int, default=DEFAULT_BROKER_PORT)
    p_http.add_argument("--strict", action="store_true")
    p_http.add_argument("--fail-open", action="store_true")
    p_http.set_defaults(func=_cmd_http)

    # wrap-config
    p_wrap = sub.add_parser("wrap-config", help="Wrap servers in claude_desktop_config.json")
    p_wrap.add_argument("--config", default=None, help="Path to claude_desktop_config.json")
    p_wrap.add_argument("--broker-host", default=DEFAULT_BROKER_HOST)
    p_wrap.add_argument("--broker-port", type=int, default=DEFAULT_BROKER_PORT)
    p_wrap.set_defaults(func=_cmd_wrap_config)

    # unwrap-config
    p_unwrap = sub.add_parser("unwrap-config", help="Restore original claude_desktop_config.json")
    p_unwrap.add_argument("--config", default=None)
    p_unwrap.set_defaults(func=_cmd_unwrap_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
