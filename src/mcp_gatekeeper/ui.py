"""Always-on-top Tkinter confirmation popup UI for MCP Gatekeeper."""

from __future__ import annotations

import json
import queue
import threading
from typing import Callable

from mcp_gatekeeper.broker import PendingApproval


class GatekeeperTkUI:
    """Thread-safe always-on-top modal queue built with Python's standard tkinter."""

    def __init__(
        self,
        on_decision: Callable[[str, bool], None],
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        self._tk = tk
        self._scrolledtext = scrolledtext
        self._on_decision = on_decision
        self._on_quit = on_quit

        self._incoming: queue.Queue[tuple[str, object]] = queue.Queue()
        self._pending_list: list[PendingApproval] = []
        self._current_index: int = 0

        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title("MCP Gatekeeper")

        self._modal: tk.Toplevel | None = None
        self._header_var = tk.StringVar()
        self._queue_var = tk.StringVar()
        self._server_var = tk.StringVar()
        self._tool_var = tk.StringVar()
        self._meta_var = tk.StringVar()
        self._params_box: scrolledtext.ScrolledText | None = None

    def enqueue_request(self, req: PendingApproval) -> None:
        """Called from the asyncio broker thread to enqueue a new confirmation popup."""
        self._incoming.put(("add", req))

    def cancel_request(self, approval_id: str) -> None:
        """Called from the asyncio broker thread if a proxy disconnects."""
        self._incoming.put(("cancel", approval_id))

    def shutdown(self) -> None:
        self._incoming.put(("shutdown", None))

    def run_mainloop(self) -> None:
        """Run the Tkinter event loop on the main thread."""
        self._poll_queue()
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            if self._on_quit is not None:
                self._on_quit()

    def _poll_queue(self) -> None:
        changed = False
        while True:
            try:
                kind, payload = self._incoming.get_nowait()
            except queue.Empty:
                break

            if kind == "add" and isinstance(payload, PendingApproval):
                self._pending_list.append(payload)
                changed = True
            elif kind == "cancel" and isinstance(payload, str):
                before = len(self._pending_list)
                self._pending_list = [
                    item for item in self._pending_list if item.approval_id != payload
                ]
                if len(self._pending_list) != before:
                    self._current_index = min(
                        self._current_index, max(0, len(self._pending_list) - 1)
                    )
                    changed = True
            elif kind == "shutdown":
                self._root.quit()
                return

        if changed:
            self._refresh_modal()

        self._root.after(75, self._poll_queue)

    def _ensure_modal(self) -> None:
        tk = self._tk
        if self._modal is not None and self._modal.winfo_exists():
            return

        win = tk.Toplevel(self._root)
        win.title("MCP Gatekeeper — Action Confirmation Required")
        win.geometry("600x470")
        win.minsize(520, 400)
        win.configure(bg="#111827")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._block_current)

        # Header bar
        top = tk.Frame(win, bg="#1F2937", padx=16, pady=12)
        top.pack(fill=tk.X)

        tk.Label(
            top,
            textvariable=self._header_var,
            font=("Segoe UI", 12, "bold"),
            fg="#F9FAFB",
            bg="#1F2937",
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(
            top,
            textvariable=self._queue_var,
            font=("Segoe UI", 10, "bold"),
            fg="#FBBF24",
            bg="#1F2937",
            anchor="e",
        ).pack(side=tk.RIGHT)

        # Metadata card
        meta = tk.Frame(win, bg="#1F2937", padx=14, pady=10)
        meta.pack(fill=tk.X, padx=16, pady=(12, 8))

        tk.Label(
            meta,
            textvariable=self._server_var,
            font=("Segoe UI", 10, "bold"),
            fg="#93C5FD",
            bg="#1F2937",
            anchor="w",
        ).pack(fill=tk.X)

        tk.Label(
            meta,
            textvariable=self._tool_var,
            font=("Segoe UI", 11, "bold"),
            fg="#FFFFFF",
            bg="#1F2937",
            anchor="w",
            pady=2,
        ).pack(fill=tk.X)

        tk.Label(
            meta,
            textvariable=self._meta_var,
            font=("Segoe UI", 9),
            fg="#9CA3AF",
            bg="#1F2937",
            anchor="w",
        ).pack(fill=tk.X)

        # Parameters viewer
        tk.Label(
            win,
            text="Request Parameters (JSON):",
            font=("Segoe UI", 9, "bold"),
            fg="#D1D5DB",
            bg="#111827",
            anchor="w",
        ).pack(fill=tk.X, padx=16, pady=(4, 2))

        params_box = self._scrolledtext.ScrolledText(
            win,
            height=11,
            font=("Consolas", 10),
            bg="#0B1120",
            fg="#E5E7EB",
            insertbackground="#E5E7EB",
            relief=tk.FLAT,
            padx=10,
            pady=8,
        )
        params_box.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))
        params_box.configure(state=tk.DISABLED)
        self._params_box = params_box

        # Bottom controls: queue navigation + Allow/Block buttons
        controls = tk.Frame(win, bg="#111827", padx=16, pady=12)
        controls.pack(fill=tk.X)

        tk.Button(
            controls,
            text="◀ Prev",
            command=self._prev_item,
            bg="#374151",
            fg="#F9FAFB",
            relief=tk.FLAT,
            padx=10,
            pady=6,
        ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Button(
            controls,
            text="Next ▶",
            command=self._next_item,
            bg="#374151",
            fg="#F9FAFB",
            relief=tk.FLAT,
            padx=10,
            pady=6,
        ).pack(side=tk.LEFT)

        tk.Button(
            controls,
            text="Allow (Enter)",
            command=self._allow_current,
            bg="#059669",
            fg="#FFFFFF",
            activebackground="#047857",
            activeforeground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=16,
            pady=8,
        ).pack(side=tk.RIGHT, padx=(8, 0))

        tk.Button(
            controls,
            text="Block (Esc)",
            command=self._block_current,
            bg="#DC2626",
            fg="#FFFFFF",
            activebackground="#B91C1C",
            activeforeground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=16,
            pady=8,
        ).pack(side=tk.RIGHT)

        win.bind("<Return>", lambda _e: self._allow_current())
        win.bind("<Escape>", lambda _e: self._block_current())
        self._modal = win

    def _refresh_modal(self) -> None:
        tk = self._tk
        if not self._pending_list:
            if self._modal is not None and self._modal.winfo_exists():
                self._modal.withdraw()
            return

        self._ensure_modal()
        assert self._modal is not None

        if self._current_index >= len(self._pending_list):
            self._current_index = len(self._pending_list) - 1

        req = self._pending_list[self._current_index]
        total = len(self._pending_list)

        self._header_var.set("MCP Tool Execution Request")
        self._queue_var.set(f"Pending {self._current_index + 1} of {total}")
        self._server_var.set(
            f"Server: {req.server_name}   |   Transport: {req.transport.upper()}"
        )
        self._tool_var.set(f"Tool / Method: {req.tool_name} ({req.method})")
        self._meta_var.set(
            f"JSON-RPC ID: {req.jsonrpc_id}   |   Received: {req.created_at}"
        )

        if self._params_box is not None:
            pretty = json.dumps(req.arguments, indent=2, ensure_ascii=False)
            self._params_box.configure(state=tk.NORMAL)
            self._params_box.delete("1.0", tk.END)
            self._params_box.insert(tk.END, pretty)
            self._params_box.configure(state=tk.DISABLED)

        self._modal.deiconify()
        self._modal.attributes("-topmost", True)
        self._modal.lift()
        self._modal.focus_force()

    def _prev_item(self) -> None:
        if len(self._pending_list) > 1:
            self._current_index = (self._current_index - 1) % len(self._pending_list)
            self._refresh_modal()

    def _next_item(self) -> None:
        if len(self._pending_list) > 1:
            self._current_index = (self._current_index + 1) % len(self._pending_list)
            self._refresh_modal()

    def _allow_current(self) -> None:
        self._decide_current(True)

    def _block_current(self) -> None:
        self._decide_current(False)

    def _decide_current(self, allowed: bool) -> None:
        if not self._pending_list:
            return
        req = self._pending_list.pop(self._current_index)
        if self._current_index >= len(self._pending_list):
            self._current_index = max(0, len(self._pending_list) - 1)
        self._on_decision(req.approval_id, allowed)
        self._refresh_modal()


def run_broker_with_tk_ui(
    broker_factory: Callable[[Callable, Callable], object],
) -> None:
    """Helper to run an asyncio broker in a background thread and Tkinter on main thread."""
    raise NotImplementedError("Use cli.py daemon entrypoint.")
