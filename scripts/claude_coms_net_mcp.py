#!/usr/bin/env python3
"""Claude Code MCP server for the Soar/Pi coms-net hub (MarinoWorkspace).

This is a genuine coms-net peer, not a wrapper around the operator CLI. It
registers a `claude-code` identity with the hub, keeps it alive with a
background heartbeat, and exposes the coms-net verbs to Claude Code as MCP
tools so Claude can discover peers (Marino, Aluna, hermes, pi-agents) and
delegate bounded work to them.

Transport is JSON-RPC 2.0 over stdio, implemented directly so this file keeps
the dependency-free property of `coms_net_client.py`. Nothing is written to
stdout except protocol frames; all diagnostics go to stderr.

Scope: outbound delegation (Claude Code -> peers). Inbound delivery
(peers -> Claude Code) needs a long-running headless runner and is handled by
a separate adapter, mirroring `hermes_coms_net_adapter.py`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coms_net_client import ComsNetClient, ComsNetConfig, ComsNetError  # noqa: E402

DEFAULT_SERVER = "http://100.95.64.78:52965"
DEFAULT_PROJECT = "soar"
DEFAULT_NAME = "claude-code"
DEFAULT_COLOR = "#D97757"
DEFAULT_MODEL = "claude-code"
DEFAULT_HEARTBEAT_SECONDS = 15
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
FALLBACK_PROTOCOL = "2025-06-18"


def log(msg: str) -> None:
    print(f"[claude-coms-net] {msg}", file=sys.stderr, flush=True)


def load_env_file(path: str | None) -> None:
    """Load KEY=VALUE lines without clobbering already-exported variables."""
    if not path:
        return
    p = Path(path).expanduser()
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def read_token() -> str:
    token_file = os.environ.get("PI_COMS_NET_AUTH_TOKEN_FILE")
    if token_file:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    token = os.environ.get("PI_COMS_NET_AUTH_TOKEN") or os.environ.get("COMS_NET_AUTH_TOKEN")
    if not token:
        raise ComsNetError(
            "missing auth token: set PI_COMS_NET_AUTH_TOKEN or PI_COMS_NET_AUTH_TOKEN_FILE"
        )
    return token


class ComsNetPeer:
    """Lazily-registered coms-net identity with a background heartbeat.

    Registration is deferred until the first tool call so an unreachable hub
    never blocks Claude Code from starting; the failure surfaces as a tool
    error instead.
    """

    def __init__(self) -> None:
        load_env_file(os.environ.get("CLAUDE_COMS_NET_ENV", "~/.config/soar/coms-net.env"))
        self.server_url = os.environ.get("PI_COMS_NET_URL") or os.environ.get(
            "PI_COMS_NET_SERVER_URL", DEFAULT_SERVER
        )
        self.project = os.environ.get("PI_COMS_NET_PROJECT", DEFAULT_PROJECT)
        self.name = os.environ.get("CLAUDE_COMS_NET_NAME", DEFAULT_NAME)
        self.purpose = os.environ.get(
            "CLAUDE_COMS_NET_PURPOSE",
            "Claude Code agent: delegates bounded coding tasks to coms-net peers",
        )
        self.color = os.environ.get("CLAUDE_COMS_NET_COLOR", DEFAULT_COLOR)
        self.model = os.environ.get("CLAUDE_COMS_NET_MODEL", DEFAULT_MODEL)
        self.explicit = os.environ.get("CLAUDE_COMS_NET_EXPLICIT", "").lower() in {"1", "true", "yes"}
        self.heartbeat_seconds = int(
            os.environ.get("CLAUDE_COMS_NET_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS)
        )
        self.session_id = os.environ.get(
            "CLAUDE_COMS_NET_SESSION_ID", f"claude-{uuid.uuid4().hex[:12]}"
        )

        self._client: ComsNetClient | None = None
        self._registered = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def client(self) -> ComsNetClient:
        if self._client is None:
            self._client = ComsNetClient(
                ComsNetConfig(self.server_url, read_token(), self.project)
            )
        return self._client

    def ensure_registered(self) -> ComsNetClient:
        client = self.client()
        with self._lock:
            if self._registered:
                return client
            client.register(
                session_id=self.session_id,
                name=self.name,
                purpose=self.purpose,
                model=self.model,
                color=self.color,
                cwd=os.getcwd(),
                explicit=self.explicit,
            )
            self._registered = True
            log(f"registered as {self.name} ({self.session_id}) on project {self.project}")
            self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat.start()
        return client

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.client().heartbeat(self.session_id, model=self.model)
            except Exception as exc:  # keep the loop alive across transient hub errors
                log(f"heartbeat failed: {exc}")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            if not self._registered:
                return
            self._registered = False
        try:
            self.client().unregister(self.session_id)
            log("unregistered")
        except Exception as exc:
            log(f"unregister failed: {exc}")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "coms_net_health",
        "description": (
            "Check the coms-net hub is reachable. Unauthenticated; use this first when "
            "diagnosing connectivity before blaming the token."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "coms_net_list",
        "description": (
            "List agents currently online in this project (e.g. Marino, Aluna, hermes, "
            "pi-agents) with their purpose and live context usage. Call this before "
            "delegating so you address a real, online peer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_explicit": {
                    "type": "boolean",
                    "description": "Include agents hidden from normal discovery. Default false.",
                }
            },
        },
    },
    {
        "name": "coms_net_send",
        "description": (
            "Send a prompt to a peer and return immediately with a msg_id. Use for "
            "fire-and-forget or when you want to keep working locally and poll later. "
            "Delegate only bounded tasks with an explicit expected output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Peer name, e.g. 'Marino'."},
                "prompt": {"type": "string", "description": "The task prompt for the peer."},
                "conversation_id": {
                    "type": "string",
                    "description": "Optional id to thread related messages together.",
                },
            },
            "required": ["target", "prompt"],
        },
    },
    {
        "name": "coms_net_get",
        "description": "Non-blocking status check for a msg_id returned by coms_net_send.",
        "inputSchema": {
            "type": "object",
            "properties": {"msg_id": {"type": "string"}},
            "required": ["msg_id"],
        },
    },
    {
        "name": "coms_net_await",
        "description": (
            "Block until a peer replies to msg_id, or the timeout elapses. Use when the "
            "reply blocks your progress; otherwise prefer coms_net_get."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "msg_id": {"type": "string"},
                "timeout_ms": {
                    "type": "integer",
                    "description": "Wait budget in milliseconds. Default 300000 (5 min).",
                },
            },
            "required": ["msg_id"],
        },
    },
    {
        "name": "coms_net_send_and_await",
        "description": (
            "Send a prompt to a peer and wait for the reply in one call. The common case "
            "for delegating a task you need the answer to before continuing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Peer name, e.g. 'Marino'."},
                "prompt": {"type": "string", "description": "The task prompt for the peer."},
                "timeout_ms": {
                    "type": "integer",
                    "description": "Wait budget in milliseconds. Default 300000 (5 min).",
                },
                "conversation_id": {"type": "string"},
            },
            "required": ["target", "prompt"],
        },
    },
]


def dispatch_tool(peer: ComsNetPeer, name: str, args: dict[str, Any]) -> Any:
    if name == "coms_net_health":
        return peer.client().health()

    if name == "coms_net_list":
        client = peer.ensure_registered()
        agents = client.list_agents(include_explicit=bool(args.get("include_explicit", False)))
        return {
            "project": peer.project,
            "self": peer.name,
            "count": len(agents),
            "agents": agents,
        }

    if name == "coms_net_send":
        client = peer.ensure_registered()
        return client.send(
            sender_session=peer.session_id,
            target=args["target"],
            prompt=args["prompt"],
            conversation_id=args.get("conversation_id"),
        )

    if name == "coms_net_get":
        return peer.ensure_registered().get_message(args["msg_id"])

    if name == "coms_net_await":
        client = peer.ensure_registered()
        return client.await_message(args["msg_id"], timeout_ms=int(args.get("timeout_ms", 300_000)))

    if name == "coms_net_send_and_await":
        client = peer.ensure_registered()
        sent = client.send(
            sender_session=peer.session_id,
            target=args["target"],
            prompt=args["prompt"],
            conversation_id=args.get("conversation_id"),
        )
        msg_id = sent.get("msg_id") if isinstance(sent, dict) else None
        if not msg_id:
            return {"error": "hub did not return a msg_id", "raw": sent}
        return client.await_message(msg_id, timeout_ms=int(args.get("timeout_ms", 300_000)))

    raise ComsNetError(f"unknown tool: {name}")


class MCPServer:
    def __init__(self, peer: ComsNetPeer) -> None:
        self.peer = peer
        self.protocol = FALLBACK_PROTOCOL

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        method = req.get("method")
        req_id = req.get("id")
        # Notifications carry no id and must never receive a response.
        if req_id is None:
            return None

        try:
            if method == "initialize":
                requested = (req.get("params") or {}).get("protocolVersion")
                if requested in SUPPORTED_PROTOCOLS:
                    self.protocol = requested
                return self.ok(
                    req_id,
                    {
                        "protocolVersion": self.protocol,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "claude-coms-net", "version": SERVER_VERSION},
                    },
                )

            if method == "ping":
                return self.ok(req_id, {})

            if method == "tools/list":
                return self.ok(req_id, {"tools": TOOLS})

            if method == "tools/call":
                params = req.get("params") or {}
                name = params.get("name", "")
                args = params.get("arguments") or {}
                try:
                    result = dispatch_tool(self.peer, name, args)
                    return self.ok(req_id, self.content(result))
                except KeyError as exc:
                    return self.ok(req_id, self.content(f"missing required argument: {exc}", True))
                except Exception as exc:
                    return self.ok(req_id, self.content(f"{type(exc).__name__}: {exc}", True))

            return self.err(req_id, -32601, f"method not found: {method}")
        except Exception as exc:  # never let one bad frame kill the server
            return self.err(req_id, -32603, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def content(payload: Any, is_error: bool = False) -> dict[str, Any]:
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    @staticmethod
    def ok(req_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def err(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def serve(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                log("dropped malformed JSON frame")
                continue
            resp = self.handle(req)
            if resp is not None:
                print(json.dumps(resp), flush=True)
        return 0


def run_check(peer: ComsNetPeer) -> int:
    """Operator smoke test: exercise the same path Claude Code uses."""
    print(f"hub     : {peer.server_url}")
    print(f"project : {peer.project}")
    print(f"identity: {peer.name} ({peer.session_id})")
    try:
        print("\nhealth  :", json.dumps(dispatch_tool(peer, "coms_net_health", {})))
    except Exception as exc:
        print(f"\nhealth  : FAILED — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    try:
        listing = dispatch_tool(peer, "coms_net_list", {})
        print(f"\npeers   : {listing['count']} online")
        for agent in listing["agents"]:
            print(f"  - {agent.get('name')}  [{agent.get('status', '?')}]  {agent.get('purpose', '')}")
        return 0
    except Exception as exc:
        print(f"\npeers   : FAILED — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        peer.shutdown()


def main() -> int:
    peer = ComsNetPeer()
    if "--check" in sys.argv[1:]:
        return run_check(peer)
    server = MCPServer(peer)
    log(f"stdio server up; hub={peer.server_url} project={peer.project} name={peer.name}")
    try:
        return server.serve()
    except KeyboardInterrupt:
        return 0
    finally:
        peer.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
