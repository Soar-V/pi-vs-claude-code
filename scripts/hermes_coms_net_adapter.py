#!/usr/bin/env python3
"""Hermes adapter daemon for the Soar/Pi coms-net hub.

This is a genuine bridge, not a Pi rename: it registers Hermes as its own
network agent, exposes the same project-scoped identity/heartbeat/SSE lifecycle,
turns inbound coms-net prompts into headless `hermes -z` runs, and submits the
final Hermes response back to the originating peer.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from coms_net_client import ComsNetClient, ComsNetConfig, ComsNetError, HttpError

DEFAULT_SERVER = "http://100.95.64.78:52965"
DEFAULT_PROJECT = "soar"
DEFAULT_MODEL = "hermes-agent"
DEFAULT_HEARTBEAT_SECONDS = 15
DEFAULT_MAX_WORKERS = 1
DEFAULT_RUN_TIMEOUT_SECONDS = 1800
MAX_HOPS = 8


@dataclass
class WorkItem:
    msg_id: str
    sender: str
    sender_session: str | None
    prompt: str
    conversation_id: str | None
    response_schema: dict[str, Any] | None
    hops: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class HermesAdapter:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        token = read_token(args)
        self.client = ComsNetClient(ComsNetConfig(server_url=args.server_url, auth_token=token, project=args.project))
        self.session_id = args.session_id or f"hermes-{uuid.uuid4().hex[:12]}"
        self.name = args.name
        self.stop = threading.Event()
        self.work: queue.Queue[WorkItem] = queue.Queue()
        self.active_lock = threading.Lock()
        self.active_count = 0
        self.seen: set[str] = set()
        self.threads: list[threading.Thread] = []

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}", flush=True)

    def start(self) -> None:
        health = self.client.health()
        self.log(f"hub ok server_id={health.get('server_id', '?') if isinstance(health, dict) else '?'}")
        reg = self.client.register(
            session_id=self.session_id,
            name=self.name,
            purpose=self.args.purpose,
            model=self.args.model_name,
            color=self.args.color,
            cwd=str(Path(self.args.workdir).resolve()),
            explicit=self.args.explicit,
        )
        self.log(f"registered name={self.name} session_id={self.session_id} project={self.args.project}")
        if isinstance(reg, dict) and reg.get("sse_url"):
            self.sse_url = reg["sse_url"]
        else:
            self.sse_url = f"/v1/events?project={self.args.project}&session_id={self.session_id}"

        self.threads.append(threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True))
        self.threads.append(threading.Thread(target=self.event_loop, name="events", daemon=True))
        for idx in range(self.args.max_workers):
            self.threads.append(threading.Thread(target=self.worker_loop, name=f"worker-{idx}", daemon=True))
        for thread in self.threads:
            thread.start()

    def run_forever(self) -> int:
        self.start()
        try:
            while not self.stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        if self.stop.is_set():
            return
        self.stop.set()
        try:
            self.client.unregister(self.session_id)
            self.log("unregistered")
        except Exception as exc:
            self.log(f"unregister failed: {exc}")

    def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            try:
                with self.active_lock:
                    active = self.active_count
                self.client.heartbeat(
                    self.session_id,
                    queue_depth=self.work.qsize() + active,
                    model=self.args.model_name,
                    status="online",
                )
            except Exception as exc:
                self.log(f"heartbeat failed: {exc}")
            self.stop.wait(self.args.heartbeat_seconds)

    def event_loop(self) -> None:
        while not self.stop.is_set():
            try:
                for event, data, _event_id in self.client.events(self.sse_url):
                    if self.stop.is_set():
                        return
                    self.handle_event(event, data)
            except Exception as exc:
                if not self.stop.is_set():
                    self.log(f"event stream failed: {exc}; reconnecting")
                    time.sleep(2)

    def handle_event(self, event: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if event in {"agent_joined", "agent_left", "agent_stale", "response"}:
            if self.args.verbose:
                self.log(f"event {event}: {json.dumps(redacted_event(data), sort_keys=True)}")
            return
        if event not in {"prompt", "message", "task"}:
            return
        msg_id = str(data.get("msg_id") or data.get("id") or "")
        if not msg_id or msg_id in self.seen:
            return
        target_session = data.get("target_session")
        target = data.get("target")
        if target_session and target_session != self.session_id:
            return
        if target and target not in {self.name, self.session_id} and target_session is None:
            return
        sender_session = data.get("sender_session")
        if sender_session == self.session_id:
            return
        hops = int(data.get("hops") or 0)
        if hops >= MAX_HOPS:
            self.submit_error(msg_id, f"hop limit reached ({hops} >= {MAX_HOPS})")
            return
        prompt = str(data.get("prompt") or data.get("content") or "")
        self.seen.add(msg_id)
        sender_value = data.get("sender") or sender_session or "unknown"
        if isinstance(sender_value, dict):
            sender_name = str(sender_value.get("name") or sender_value.get("session_id") or "unknown")
        else:
            sender_name = str(sender_value)
        item = WorkItem(
            msg_id=msg_id,
            sender=sender_name,
            sender_session=str(sender_session) if sender_session else None,
            prompt=prompt,
            conversation_id=data.get("conversation_id"),
            response_schema=data.get("response_schema") if isinstance(data.get("response_schema"), dict) else None,
            hops=hops,
            raw=data,
        )
        self.work.put(item)
        self.log(f"queued inbound msg_id={msg_id} from={item.sender} queue={self.work.qsize()}")

    def worker_loop(self) -> None:
        while not self.stop.is_set():
            try:
                item = self.work.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.active_lock:
                self.active_count += 1
            try:
                self.process_item(item)
            finally:
                with self.active_lock:
                    self.active_count -= 1
                self.work.task_done()

    def process_item(self, item: WorkItem) -> None:
        self.log(f"running Hermes for msg_id={item.msg_id}")
        try:
            response, error = self.run_hermes(item)
        except Exception as exc:
            response, error = None, str(exc)
        if item.response_schema and error is None:
            try:
                response = json.loads(response if isinstance(response, str) else json.dumps(response))
            except Exception:
                error = "response not valid JSON"
                response = None
        try:
            self.client.submit_response(msg_id=item.msg_id, responder_session=self.session_id, response=response, error=error)
            self.log(f"submitted response msg_id={item.msg_id} error={bool(error)}")
        except Exception as exc:
            self.log(f"submit response failed msg_id={item.msg_id}: {exc}")

    def run_hermes(self, item: WorkItem) -> tuple[Any, str | None]:
        if self.args.mock_response is not None:
            return self.args.mock_response.format(prompt=item.prompt, sender=item.sender, msg_id=item.msg_id), None
        prompt = build_prompt(item, self.name, self.args.project)
        cmd = [self.args.hermes_bin, "-z", prompt, "--accept-hooks"]
        if self.args.toolsets:
            cmd.extend(["--toolsets", self.args.toolsets])
        if self.args.skills:
            cmd.extend(["--skills", self.args.skills])
        env = os.environ.copy()
        env.setdefault("HERMES_COMS_NET_SESSION_ID", self.session_id)
        env.setdefault("HERMES_COMS_NET_NAME", self.name)
        proc = subprocess.run(
            cmd,
            cwd=self.args.workdir,
            text=True,
            capture_output=True,
            timeout=self.args.run_timeout_seconds,
            env=env,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or f"hermes exited {proc.returncode}").strip()
            return None, err[-4000:]
        return proc.stdout.strip(), None

    def submit_error(self, msg_id: str, error: str) -> None:
        try:
            self.client.submit_response(msg_id=msg_id, responder_session=self.session_id, response=None, error=error)
        except Exception as exc:
            self.log(f"failed to submit error for {msg_id}: {exc}")


def build_prompt(item: WorkItem, name: str, project: str) -> str:
    schema_note = ""
    if item.response_schema:
        schema_note = "\nThe sender requested JSON matching this schema. Reply with valid JSON only:\n" + json.dumps(item.response_schema, indent=2)
    return (
        f"You are Hermes network agent '{name}' on private coms-net project '{project}'.\n"
        f"A peer agent sent you a delegated prompt. Answer the peer directly and concisely.\n"
        f"Do not mention implementation details unless needed.\n\n"
        f"Peer: {item.sender}\n"
        f"Message id: {item.msg_id}\n"
        f"Conversation id: {item.conversation_id or 'none'}\n"
        f"Hops: {item.hops}\n"
        f"{schema_note}\n\n"
        f"Peer prompt:\n{item.prompt}"
    )


def read_token(args: argparse.Namespace) -> str:
    if args.auth_token:
        return args.auth_token
    if args.auth_token_file:
        return Path(args.auth_token_file).read_text(encoding="utf-8").strip()
    env_token = os.environ.get("PI_COMS_NET_AUTH_TOKEN") or os.environ.get("COMS_NET_AUTH_TOKEN")
    if env_token:
        return env_token
    raise SystemExit("missing auth token: set PI_COMS_NET_AUTH_TOKEN or pass --auth-token-file")


def redacted_event(data: dict[str, Any]) -> dict[str, Any]:
    clean = dict(data)
    for key in list(clean):
        if "token" in key.lower() or "secret" in key.lower() or "auth" in key.lower():
            clean[key] = "[REDACTED]"
    return clean


def load_env_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Hermes as a coms-net agent")
    p.add_argument("--env-file", default=os.environ.get("HERMES_COMS_NET_ENV", os.path.expanduser("~/.config/soar/coms-net.env")))
    p.add_argument("--server-url", default=os.environ.get("PI_COMS_NET_URL", DEFAULT_SERVER))
    p.add_argument("--project", default=os.environ.get("PI_COMS_NET_PROJECT", DEFAULT_PROJECT))
    p.add_argument("--auth-token", default=None, help=argparse.SUPPRESS)
    p.add_argument("--auth-token-file", default=os.environ.get("PI_COMS_NET_AUTH_TOKEN_FILE"))
    p.add_argument("--name", default=os.environ.get("HERMES_COMS_NET_NAME", "hermes-main"))
    p.add_argument("--session-id", default=os.environ.get("HERMES_COMS_NET_SESSION_ID"))
    p.add_argument("--purpose", default=os.environ.get("HERMES_COMS_NET_PURPOSE", "Hermes adapter: receives delegated coms-net tasks and answers via headless Hermes runs"))
    p.add_argument("--model-name", default=os.environ.get("HERMES_COMS_NET_MODEL", DEFAULT_MODEL))
    p.add_argument("--color", default=os.environ.get("HERMES_COMS_NET_COLOR", "#7c3aed"))
    p.add_argument("--hermes-bin", default=os.environ.get("HERMES_BIN", "hermes"))
    p.add_argument("--workdir", default=os.environ.get("HERMES_COMS_NET_WORKDIR", os.getcwd()))
    p.add_argument("--toolsets", default=os.environ.get("HERMES_COMS_NET_TOOLSETS", "terminal,file,web"))
    p.add_argument("--skills", default=os.environ.get("HERMES_COMS_NET_SKILLS", ""))
    p.add_argument("--heartbeat-seconds", type=int, default=int(os.environ.get("HERMES_COMS_NET_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS)))
    p.add_argument("--max-workers", type=int, default=int(os.environ.get("HERMES_COMS_NET_MAX_WORKERS", DEFAULT_MAX_WORKERS)))
    p.add_argument("--run-timeout-seconds", type=int, default=int(os.environ.get("HERMES_COMS_NET_RUN_TIMEOUT_SECONDS", DEFAULT_RUN_TIMEOUT_SECONDS)))
    p.add_argument("--explicit", action="store_true", default=os.environ.get("HERMES_COMS_NET_EXPLICIT", "").lower() in {"1", "true", "yes"})
    p.add_argument("--mock-response", default=os.environ.get("HERMES_COMS_NET_MOCK_RESPONSE"), help="testing only: respond without invoking Hermes")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    load_env_file(args.env_file)
    # Apply env-file values after loading the file, while preserving explicit CLI values.
    defaults: dict[str, Any] = {
        "server_url": DEFAULT_SERVER,
        "project": DEFAULT_PROJECT,
        "auth_token_file": None,
        "name": "hermes-main",
        "session_id": None,
        "purpose": "Hermes adapter: receives delegated coms-net tasks and answers via headless Hermes runs",
        "model_name": DEFAULT_MODEL,
        "color": "#7c3aed",
        "hermes_bin": "hermes",
        "workdir": os.getcwd(),
        "toolsets": "terminal,file,web",
        "skills": "",
        "heartbeat_seconds": DEFAULT_HEARTBEAT_SECONDS,
        "max_workers": DEFAULT_MAX_WORKERS,
        "run_timeout_seconds": DEFAULT_RUN_TIMEOUT_SECONDS,
        "mock_response": None,
    }
    env_map: dict[str, tuple[str, Callable[[str], Any]]] = {
        "server_url": ("PI_COMS_NET_URL", str),
        "project": ("PI_COMS_NET_PROJECT", str),
        "auth_token_file": ("PI_COMS_NET_AUTH_TOKEN_FILE", str),
        "name": ("HERMES_COMS_NET_NAME", str),
        "session_id": ("HERMES_COMS_NET_SESSION_ID", str),
        "purpose": ("HERMES_COMS_NET_PURPOSE", str),
        "model_name": ("HERMES_COMS_NET_MODEL", str),
        "color": ("HERMES_COMS_NET_COLOR", str),
        "hermes_bin": ("HERMES_BIN", str),
        "workdir": ("HERMES_COMS_NET_WORKDIR", str),
        "toolsets": ("HERMES_COMS_NET_TOOLSETS", str),
        "skills": ("HERMES_COMS_NET_SKILLS", str),
        "heartbeat_seconds": ("HERMES_COMS_NET_HEARTBEAT_SECONDS", int),
        "max_workers": ("HERMES_COMS_NET_MAX_WORKERS", int),
        "run_timeout_seconds": ("HERMES_COMS_NET_RUN_TIMEOUT_SECONDS", int),
        "mock_response": ("HERMES_COMS_NET_MOCK_RESPONSE", str),
    }
    for attr, (env_name, caster) in env_map.items():
        if getattr(args, attr) == defaults[attr] and os.environ.get(env_name) is not None:
            setattr(args, attr, caster(os.environ[env_name]))
    if not args.explicit and os.environ.get("HERMES_COMS_NET_EXPLICIT", "").lower() in {"1", "true", "yes"}:
        args.explicit = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    adapter = HermesAdapter(args)

    def _stop(_sig: int, _frame: Any) -> None:
        adapter.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return adapter.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
