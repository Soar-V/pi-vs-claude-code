#!/usr/bin/env python3
"""Reusable authenticated Python client for the Soar/Pi coms-net hub.

No third-party dependencies.  Shared by the Hermes adapter daemon, CLI tools,
and tests so every framework integration uses the same HTTP/SSE lifecycle.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator


class ComsNetError(RuntimeError):
    pass


class HttpError(ComsNetError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        detail = body.get("error") if isinstance(body, dict) else str(body)
        super().__init__(f"HTTP {status}: {detail}")


@dataclass
class ComsNetConfig:
    server_url: str
    auth_token: str
    project: str = "soar"
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self.server_url = self.server_url.rstrip("/")
        if not self.server_url:
            raise ValueError("server_url is required")
        if not self.auth_token:
            raise ValueError("auth_token is required")


class ComsNetClient:
    def __init__(self, config: ComsNetConfig):
        self.config = config

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.config.server_url + path

    def request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        timeout: float | None = None,
        auth: bool = True,
    ) -> Any:
        headers = {"accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        if auth:
            headers["authorization"] = f"Bearer {self.config.auth_token}"
        req = urllib.request.Request(self._url(path), data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.config.timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            raise HttpError(exc.code, parsed) from exc
        except urllib.error.URLError as exc:
            raise ComsNetError(str(exc.reason)) from exc

    def health(self) -> Any:
        return self.request("GET", "/health", auth=False)

    def register(
        self,
        *,
        session_id: str,
        name: str,
        purpose: str = "",
        model: str = "hermes",
        color: str = "#7c3aed",
        cwd: str = ".",
        explicit: bool = False,
    ) -> Any:
        return self.request(
            "POST",
            "/v1/agents/register",
            {
                "project": self.config.project,
                "session_id": session_id,
                "name": name,
                "purpose": purpose,
                "model": model,
                "color": color,
                "cwd": cwd,
                "explicit": explicit,
            },
        )

    def heartbeat(
        self,
        session_id: str,
        *,
        context_used_pct: int = 0,
        queue_depth: int = 0,
        model: str = "hermes",
        status: str = "online",
    ) -> Any:
        return self.request(
            "POST",
            f"/v1/agents/{urllib.parse.quote(session_id)}/heartbeat",
            {
                "project": self.config.project,
                "context_used_pct": context_used_pct,
                "queue_depth": queue_depth,
                "model": model,
                "status": status,
            },
            timeout=10,
        )

    def unregister(self, session_id: str) -> Any:
        return self.request(
            "DELETE",
            f"/v1/agents/{urllib.parse.quote(session_id)}?project={urllib.parse.quote(self.config.project)}",
            timeout=10,
        )

    def list_agents(self, *, include_explicit: bool = False) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode({"project": self.config.project, "include_explicit": str(include_explicit).lower()})
        data = self.request("GET", f"/v1/agents?{qs}")
        return list(data.get("agents", [])) if isinstance(data, dict) else []

    def send(
        self,
        *,
        sender_session: str,
        target: str,
        prompt: str,
        conversation_id: str | None = None,
        response_schema: dict[str, Any] | None = None,
        hops: int = 0,
    ) -> Any:
        return self.request(
            "POST",
            "/v1/messages",
            {
                "project": self.config.project,
                "sender_session": sender_session,
                "target": target,
                "target_session": None,
                "prompt": prompt,
                "conversation_id": conversation_id,
                "response_schema": response_schema,
                "hops": hops,
            },
        )

    def get_message(self, msg_id: str) -> Any:
        return self.request("GET", f"/v1/messages/{urllib.parse.quote(msg_id)}")

    def await_message(self, msg_id: str, *, timeout_ms: int = 1_800_000) -> Any:
        qs = urllib.parse.urlencode({"timeout_ms": timeout_ms})
        return self.request("GET", f"/v1/messages/{urllib.parse.quote(msg_id)}/await?{qs}", timeout=(timeout_ms / 1000) + 10)

    def submit_response(self, *, msg_id: str, responder_session: str, response: Any = None, error: str | None = None) -> Any:
        return self.request(
            "POST",
            f"/v1/messages/{urllib.parse.quote(msg_id)}/response",
            {
                "project": self.config.project,
                "responder_session": responder_session,
                "response": response,
                "error": error,
            },
        )

    def events(self, sse_url: str, *, reconnect_delay: float = 2.0) -> Iterator[tuple[str, Any, str | None]]:
        """Yield SSE events forever. Caller handles shutdown by breaking/raising KeyboardInterrupt."""
        url = self._url(sse_url)
        headers = {"authorization": f"Bearer {self.config.auth_token}", "accept": "text/event-stream"}
        while True:
            req = urllib.request.Request(url, method="GET", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=None) as resp:
                    event = "message"
                    data_lines: list[str] = []
                    event_id: str | None = None
                    for raw in resp:
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line:
                            if data_lines:
                                text = "\n".join(data_lines)
                                try:
                                    data: Any = json.loads(text)
                                except json.JSONDecodeError:
                                    data = text
                                yield event, data, event_id
                            event = "message"
                            data_lines = []
                            event_id = None
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            value = line[5:]
                            if value.startswith(" "):
                                value = value[1:]
                            data_lines.append(value)
                        elif line.startswith("id:"):
                            event_id = line[3:].strip()
            except KeyboardInterrupt:
                raise
            except Exception:
                time.sleep(reconnect_delay)


def event_loop_once(events: Iterator[tuple[str, Any, str | None]], handler: Callable[[str, Any, str | None], None]) -> None:
    event, data, event_id = next(events)
    handler(event, data, event_id)
