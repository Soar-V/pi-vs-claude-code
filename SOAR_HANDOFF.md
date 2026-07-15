# Soar Pi Agent Network — Handoff

This fork is Soar's durable copy of `disler/pi-vs-claude-code`, with an operational guide for the `coms-net` HTTP/SSE agent network.

## Start here

1. Read [`docs/SOAR-COMS-NET.md`](docs/SOAR-COMS-NET.md).
2. The hub implementation is [`scripts/coms-net-server.ts`](scripts/coms-net-server.ts).
3. The Pi client extension is [`extensions/coms-net.ts`](extensions/coms-net.ts).
4. A production systemd template is [`deploy/pi-coms-net.service`](deploy/pi-coms-net.service).
5. Never commit the bearer token or provider API keys. Keep the hub token in a root-readable environment file and each client's provider key in its local secret store.

## Current Soar topology

The intended topology is one hub on the main Hermes/Meridian server, reachable privately over Tailscale. Every participating computer runs one or more Pi clients and points them at the hub URL. Pi agents share a `project` namespace and can then discover and message one another.

The protocol provides four Pi tools:

- `coms_net_list`
- `coms_net_send`
- `coms_net_get`
- `coms_net_await`

This is a live-message substrate, not shared memory or an autonomous scheduler. Agents only answer while their Pi sessions are running.

## Rules for agents modifying this fork

- Preserve the upstream remote and make Soar-specific changes on the fork.
- Do not weaken bearer authentication or expose the hub directly to the public internet.
- Do not log bearer tokens or prompt bodies in added audit logs.
- Verify server changes with a real `/health` call and an authenticated register/list/message flow.
- Keep the original `coms` same-host extension untouched unless the task explicitly concerns it.
