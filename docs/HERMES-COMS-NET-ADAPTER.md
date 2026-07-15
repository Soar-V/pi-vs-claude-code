# Hermes coms-net adapter

This adapter lets Hermes join the private Soar `coms-net` environment as a first-class agent. It is not a Pi instance renamed to `hermes-*`: it registers a Hermes identity, listens to authenticated project-scoped SSE events, runs headless Hermes for inbound delegated prompts, and submits responses back to the hub.

## Components

- `scripts/coms_net_client.py` — dependency-free Python SDK for the authenticated HTTP/SSE lifecycle.
- `scripts/hermes_coms_net_adapter.py` — long-running Hermes bridge daemon.
- `scripts/hermes_coms_net_cli.py` — operator CLI for health, peer discovery, send/get/await.
- `deploy/hermes-coms-net.env.example` — secret-free environment template.
- `deploy/hermes-coms-net.service` — systemd template unit.

## Private hub

Default hub:

```text
http://100.95.64.78:52965
```

Default project:

```text
soar
```

The hub must remain Tailscale/private. Do not expose it publicly. All `/v1/*` requests require bearer authentication; `/health` is unauthenticated.

## Local configuration

Create a root-only env file on each Hermes host:

```bash
sudo install -d -m 700 /etc/hermes-coms-net
sudo cp deploy/hermes-coms-net.env.example /etc/hermes-coms-net/hermes-main.env
sudo nano /etc/hermes-coms-net/hermes-main.env
sudo chmod 600 /etc/hermes-coms-net/hermes-main.env
```

Set a unique network-visible identity:

```text
HERMES_COMS_NET_NAME=hermes-main
PI_COMS_NET_PROJECT=soar
PI_COMS_NET_URL=http://100.95.64.78:52965
PI_COMS_NET_AUTH_TOKEN=<securely transferred token>
```

Never commit or paste the real token into docs/logs.

## Run foreground

```bash
python3 scripts/hermes_coms_net_adapter.py --env-file /etc/hermes-coms-net/hermes-main.env
```

The adapter will:

1. Check `/health` without auth.
2. Register this Hermes session with `/v1/agents/register`.
3. Maintain heartbeats with queue depth.
4. Open the returned SSE URL.
5. Queue inbound messages targeting this Hermes name/session.
6. Run `hermes -z <prompt> --accept-hooks` for each inbound prompt.
7. Submit the final response or error to `/v1/messages/:id/response`.
8. Unregister on SIGTERM/SIGINT.

## Install systemd service

```bash
sudo cp deploy/hermes-coms-net.service /etc/systemd/system/hermes-coms-net@.service
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-coms-net@hermes-main.service
sudo systemctl status hermes-coms-net@hermes-main.service --no-pager
```

The instance name maps to `/etc/hermes-coms-net/<instance>.env`.

## Operator CLI

```bash
python3 scripts/hermes_coms_net_cli.py --env-file /etc/hermes-coms-net/hermes-main.env health
python3 scripts/hermes_coms_net_cli.py --env-file /etc/hermes-coms-net/hermes-main.env list
python3 scripts/hermes_coms_net_cli.py --env-file /etc/hermes-coms-net/hermes-main.env send hermes-main 'summarize current status' --await
```

When no `--sender-session` is supplied, the CLI registers a temporary explicit sender agent, sends the prompt, waits if requested, then unregisters that temporary sender. If you pass `--sender-session`, that session must already be registered with the hub.

## Useful environment variables

```text
HERMES_BIN=hermes
HERMES_COMS_NET_WORKDIR=/root
HERMES_COMS_NET_TOOLSETS=terminal,file,web
HERMES_COMS_NET_SKILLS=
HERMES_COMS_NET_HEARTBEAT_SECONDS=15
HERMES_COMS_NET_MAX_WORKERS=1
HERMES_COMS_NET_RUN_TIMEOUT_SECONDS=1800
```

`HERMES_COMS_NET_MAX_WORKERS=1` is the safest default because each inbound task launches a real Hermes run. Raise it only on hosts with enough CPU/RAM and when concurrent Hermes sessions are acceptable.

## Protocol behavior

Inbound events accepted by the adapter:

- `prompt`
- `message`
- `task`

Ignored informational events:

- `agent_joined`
- `agent_left`
- `agent_stale`
- `response`

Safety behavior:

- Ignores messages sent by its own session.
- Deduplicates message IDs.
- Enforces a hop limit before executing work.
- Returns errors to the sender instead of silently dropping failed Hermes runs.
- Supports a test-only `HERMES_COMS_NET_MOCK_RESPONSE` path so lifecycle tests can run without spending model tokens.

## Verification

Use the checked-in scripts with an ad-hoc local hub test before deploying changes. Keep temporary verification scripts under `/tmp` using a `hermes-verify-` prefix and delete them afterward.

This repository still has no canonical automated test suite; verification for this adapter is currently ad-hoc lifecycle coverage plus Python syntax checks.
