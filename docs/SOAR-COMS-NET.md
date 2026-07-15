# Soar coms-net setup and operations

## What this builds

`coms-net` connects independent Pi Coding Agent sessions through one Bun HTTP/SSE hub. The hub holds an in-memory registry and in-flight messages. Each Pi client:

1. registers its identity;
2. opens an SSE stream;
3. sends a heartbeat every 10 seconds;
4. receives inbound prompts and runs a normal Pi turn;
5. posts the final assistant response back to the sender.

A sender can fire-and-forget, poll, or block waiting for the response.

## Topology

```text
Pi agent: planner ─┐
Pi agent: builder ─┼── HTTPS or private Tailscale HTTP ── coms-net hub
Pi agent: reviewer ┘
```

Use one `project` namespace per collaboration pool. Agents in different project namespaces do not appear in one another's default lists.

## Security model

- `/health` is unauthenticated.
- All `/v1/*` endpoints require an `Authorization: Bearer <token>` header.
- A non-loopback bind is refused unless `PI_COMS_NET_AUTH_TOKEN` is set.
- Keep the hub on Tailscale or behind TLS. Do not expose raw HTTP to the public internet.
- The hub is in-memory. Restarting it clears agent registrations and in-flight messages.
- Default maximum forwarding depth is five hops.
- Provider API keys stay on each client; they never belong on the hub unless the hub also runs Pi clients.

## Hub install (Ubuntu/Linux)

Prerequisites: Git, Bun, and systemd.

```bash
git clone https://github.com/Soar-V/pi-vs-claude-code.git /opt/pi-vs-claude-code
cd /opt/pi-vs-claude-code
bun install
sudo install -d -m 700 /etc/pi-coms-net
sudo cp deploy/pi-coms-net.env.example /etc/pi-coms-net/default.env
sudo chmod 600 /etc/pi-coms-net/default.env
sudo editor /etc/pi-coms-net/default.env
sudo cp deploy/pi-coms-net.service /etc/systemd/system/pi-coms-net.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-coms-net
curl -fsS http://127.0.0.1:52965/health
```

Generate the token locally:

```bash
openssl rand -hex 32
```

Put it in `/etc/pi-coms-net/default.env`; never paste it into GitHub, Discord, or a command that will be saved in a shared transcript.

If the hub binds only to a Tailscale address, run the health check using that address. The checked-in service template defaults to loopback for safety; set `PI_COMS_NET_HOST` in the env file to the hub's private Tailscale IP when remote clients must connect.

## Client install (every participating computer)

```bash
git clone https://github.com/Soar-V/pi-vs-claude-code.git ~/pi-vs-claude-code
cd ~/pi-vs-claude-code
bun install
bun add --global @earendil-works/pi-coding-agent
pi --version
```

Use the maintained `@earendil-works/pi-coding-agent` package. The archived `@mariozechner/pi-coding-agent` line ended at 0.73.1 and has published security advisories; do not install it on shared systems.

Also configure at least one Pi model provider according to Pi's provider documentation. Keep that API key local to the computer.

Export the network configuration in a private shell profile or secret manager:

```bash
export PI_COMS_NET_SERVER_URL='http://TAILSCALE_HUB_IP:52965'
export PI_COMS_NET_AUTH_TOKEN='REPLACE_WITH_SHARED_SECRET'
export PI_COMS_NET_PROJECT='soar'
```

Do not put the real token in the repository's `.env`.

## Launch agents

From the repository root:

```bash
pi -e extensions/coms-net.ts \
   -e extensions/minimal.ts \
   -e extensions/theme-cycler.ts \
   --name planner --cname planner \
   --purpose 'Plans work and delegates bounded tasks' \
   --project soar --color '#36F9F6'
```

Launch another session on the same or a different machine:

```bash
pi -e extensions/coms-net.ts \
   -e extensions/minimal.ts \
   -e extensions/theme-cycler.ts \
   --name builder --cname builder \
   --purpose 'Implements and tests assigned work' \
   --project soar --color '#72F1B8'
```

Important: `--name` belongs to Pi; `--cname` belongs to coms-net. Passing both keeps the displayed Pi session name and network identity aligned.

Supported client flags:

- `--cname NAME`
- `--purpose TEXT`
- `--project NAME`
- `--color '#RRGGBB'`
- `--explicit` to hide from normal discovery while remaining exactly addressable
- `--server-url URL`
- `--auth-token TOKEN` (environment variable is preferred)

## How agents communicate

Inside Pi, the extension registers:

- `coms_net_list`: list peers and live context usage.
- `coms_net_send`: send a prompt and receive a `msg_id` after acknowledgement.
- `coms_net_get`: non-blocking status check for a `msg_id`.
- `coms_net_await`: wait for a reply; the server's request default is 30 seconds while message TTL defaults to 30 minutes.

Suggested instruction for each participating agent:

```text
You are connected to the Soar Pi network. Before duplicating work, call
coms_net_list. Delegate only bounded tasks with explicit expected output.
After coms_net_send, either continue useful local work and poll with
coms_net_get, or use coms_net_await when the reply blocks progress. Never
bounce the same task between agents; respect the hop limit and summarize
all externally obtained conclusions before acting on them.
```

Slash-command diagnostics:

```text
/coms-net
/coms-net --server
/coms-net --reconnect
/coms-net --all
/coms-net --project soar
```

## Hub API reference

- `GET /health` — no auth
- `POST /v1/agents/register`
- `GET /v1/events?project=...&session_id=...` — SSE
- `GET /v1/agents?project=...`
- `POST /v1/agents/:session_id/heartbeat`
- `DELETE /v1/agents/:session_id?project=...`
- `POST /v1/messages`
- `GET /v1/messages/:msg_id`
- `GET /v1/messages/:msg_id/await?timeout_ms=...`
- `POST /v1/messages/:msg_id/response`

## Operations

```bash
sudo systemctl status pi-coms-net
sudo journalctl -u pi-coms-net -f
sudo systemctl restart pi-coms-net
curl -fsS http://127.0.0.1:52965/health
```

The hub writes discovery metadata under `~/.pi/coms-net/projects/<project>/server.json`. It writes `server.secret.json` only when it auto-generates a loopback-only token. A service using an explicit environment token does not write the token there.

Useful tunables:

- `PI_COMS_NET_HOST`
- `PI_COMS_NET_PORT`
- `PI_COMS_NET_PUBLIC_URL`
- `PI_COMS_NET_PROJECT`
- `PI_COMS_NET_MAX_HOPS` (default 5)
- `PI_COMS_NET_MESSAGE_TTL_MS` (default 1,800,000)
- `PI_COMS_NET_MAX_INBOX` (default 100)
- `PI_COMS_NET_HEARTBEAT_MS` (default 10,000)
- `PI_COMS_NET_STALE_AFTER_MS` (default 30,000)
- `PI_COMS_NET_OFFLINE_AFTER_MS` (default 60,000)
- `PI_COMS_NET_LOG_HEARTBEAT=1` for verbose heartbeat logs
- `PI_COMS_NET_LOG_QUIET=1` for quiet logs

## Troubleshooting

`no server URL`
: Set `PI_COMS_NET_SERVER_URL`, pass `--server-url`, or run a local hub so `server.json` can be auto-discovered.

`no auth token`
: Set `PI_COMS_NET_AUTH_TOKEN`, pass `--auth-token`, or verify the local loopback hub wrote a mode-0600 `server.secret.json`.

`register failed: 401`
: Client and server tokens differ.

Peer becomes stale/offline
: Verify the Pi process is still running, the SSE path is reachable, and no proxy buffers or terminates SSE. Confirm host clocks and heartbeat configuration are reasonable.

Hub starts but remote clients cannot connect
: Verify the bind address, Tailscale ACL/firewall, port 52965, and that the URL points to a reachable private address. Do not solve this by opening the port to the public internet.

`pi` not found after Bun global install
: Add `~/.bun/bin` to `PATH` and restart the shell.

## Updating from upstream

The Soar repository is a fork. Maintain an `upstream` remote:

```bash
git remote add upstream https://github.com/disler/pi-vs-claude-code.git
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

Review upstream changes to `coms-net.ts`, `coms-net-server.ts`, `justfile`, and `.env.sample` before deploying.
