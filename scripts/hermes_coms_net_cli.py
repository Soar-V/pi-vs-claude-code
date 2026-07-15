#!/usr/bin/env python3
"""Small operator CLI for Hermes/coms-net.

Use this from cron, shell scripts, or a local Hermes tool call to inspect peers,
send prompts, and wait for replies without running the full adapter daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from coms_net_client import ComsNetClient, ComsNetConfig

DEFAULT_SERVER = "http://100.95.64.78:52965"
DEFAULT_PROJECT = "soar"


def load_env_file(path: str | None) -> None:
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


def token(args: argparse.Namespace) -> str:
    if args.auth_token_file:
        return Path(args.auth_token_file).read_text(encoding="utf-8").strip()
    t = os.environ.get("PI_COMS_NET_AUTH_TOKEN") or os.environ.get("COMS_NET_AUTH_TOKEN")
    if not t:
        raise SystemExit("missing token: set PI_COMS_NET_AUTH_TOKEN or --auth-token-file")
    return t


def client(args: argparse.Namespace) -> ComsNetClient:
    return ComsNetClient(ComsNetConfig(args.server_url, token(args), args.project))


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Hermes coms-net CLI")
    p.add_argument("--env-file", default=os.path.expanduser("~/.config/soar/coms-net.env"))
    p.add_argument("--server-url", default=os.environ.get("PI_COMS_NET_URL", DEFAULT_SERVER))
    p.add_argument("--project", default=os.environ.get("PI_COMS_NET_PROJECT", DEFAULT_PROJECT))
    p.add_argument("--auth-token-file", default=os.environ.get("PI_COMS_NET_AUTH_TOKEN_FILE"))
    p.add_argument("--session-id", default=os.environ.get("HERMES_COMS_NET_SESSION_ID"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    a_list = sub.add_parser("list")
    a_list.add_argument("--all", action="store_true")
    a_send = sub.add_parser("send")
    a_send.add_argument("target")
    a_send.add_argument("prompt")
    a_send.add_argument("--sender-session", default=None)
    a_send.add_argument("--await", dest="wait", action="store_true")
    a_send.add_argument("--timeout-ms", type=int, default=1_800_000)
    a_get = sub.add_parser("get")
    a_get.add_argument("msg_id")
    a_await = sub.add_parser("await")
    a_await.add_argument("msg_id")
    a_await.add_argument("--timeout-ms", type=int, default=1_800_000)
    args = p.parse_args(argv)
    load_env_file(args.env_file)
    if args.server_url == DEFAULT_SERVER and os.environ.get("PI_COMS_NET_URL"):
        args.server_url = os.environ["PI_COMS_NET_URL"]
    if args.project == DEFAULT_PROJECT and os.environ.get("PI_COMS_NET_PROJECT"):
        args.project = os.environ["PI_COMS_NET_PROJECT"]
    c = client(args)
    if args.cmd == "health":
        print_json(c.health())
    elif args.cmd == "list":
        print_json(c.list_agents(include_explicit=args.all))
    elif args.cmd == "send":
        generated_sender = args.sender_session is None and args.session_id is None
        sender = args.sender_session or args.session_id or f"hermes-cli-{uuid.uuid4().hex[:8]}"
        if generated_sender:
            c.register(
                session_id=sender,
                name=sender,
                purpose="Hermes coms-net CLI temporary sender",
                model="hermes-cli",
                color="#6b7280",
                cwd=os.getcwd(),
                explicit=True,
            )
        try:
            resp = c.send(sender_session=sender, target=args.target, prompt=args.prompt)
            if args.wait:
                msg_id = resp["msg_id"]
                print_json(c.await_message(msg_id, timeout_ms=args.timeout_ms))
            else:
                print_json(resp)
        finally:
            if generated_sender:
                try:
                    c.unregister(sender)
                except Exception:
                    pass
    elif args.cmd == "get":
        print_json(c.get_message(args.msg_id))
    elif args.cmd == "await":
        print_json(c.await_message(args.msg_id, timeout_ms=args.timeout_ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
