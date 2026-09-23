# technitium-docker-reconciler

Auto-creates Technitium DNS A records for containers exposed via
[caddy-docker-proxy](https://github.com/lucaslorentz/caddy-docker-proxy),
so a new service only needs **one** declaration -- labels on its compose
service -- to get both reverse-proxy routing and a matching DNS record.
No separate `hosts.yml` entry required for anything running this way.

Same tag-and-prune model as the `uptime-kuma-reconciler` in `homelab-k8s`:
every record this tool creates is marked with a `comments` value of
`managed-by=technitium-docker-reconciler`. Each pass only ever adds or
removes records carrying that exact marker; anything created by hand
through the Technitium UI, or by the existing `technitium_dns_record`
Ansible role in `homelab2`, is never touched.

## How it works

1. Connects to a Docker-API-compatible socket (Docker or Podman) and lists
   running containers.
2. Reads each container's `caddy` / `caddy_N` labels (caddy-docker-proxy's
   site-block labels -- nested directive labels like `caddy.reverse_proxy`
   are ignored) to build the desired set of hostnames.
3. Logs into Technitium's HTTP API and ensures an A record exists for each
   desired hostname, pointing at `TARGET_IP`.
4. Deletes any record it previously created (matched by the `comments`
   marker) whose source container is no longer present.
5. Repeats on `POLL_INTERVAL` (default 60s), or runs once and exits with
   `--once` for use under an external timer instead.

## Known limitation

Record tagging depends on Technitium's `comments` field, added in
relatively recent Technitium releases. **Verify your server version
returns `comments` on `zones/records/get` before relying on the prune
step** -- if it doesn't, this tool fails *safe*: it will simply never
recognize any record as managed, so it will keep adding records but never
delete anything, rather than mis-identifying and deleting manual records.
Run with `DRY_RUN=true` first against your live instance to confirm.

## Podman

Designed to be engine-agnostic from the start -- it uses whatever socket
`docker.from_env()` resolves (the `DOCKER_HOST` env var), so point it at
Podman's socket rather than Docker's:

```
# rootful
DOCKER_HOST=unix:///run/podman/podman.sock
# rootless
DOCKER_HOST=unix:///run/user/<uid>/podman/podman.sock
```

## Configuration (env vars)

| Var | Required | Default | Notes |
|---|---|---|---|
| `TECHNITIUM_HOST` | yes | -- | e.g. `10.0.0.9` |
| `TECHNITIUM_PORT` | no | `5380` | |
| `TECHNITIUM_USER` | yes | -- | from 1Password `technitium` item |
| `TECHNITIUM_PASSWORD` | yes | -- | from 1Password `technitium` item |
| `TARGET_IP` | yes | -- | LAN IP of the Docker/Podman host this reconciler watches (e.g. `10.0.0.9` for `pi`) |
| `DEFAULT_ZONE` | no | `mfmseth.com` | |
| `TTL` | no | `3600` | |
| `POLL_INTERVAL` | no | `60` (seconds) | ignored with `--once` |
| `DRY_RUN` | no | `false` | logs intended changes, makes none |
| `DOCKER_HOST` | no | docker-py default | set to a Podman socket to target Podman |

## Running

One instance per Docker/Podman host you want covered (each needs its own
`TARGET_IP`) -- e.g. one on `pi`, and another on `media` once/if it gets a
caddy-docker-proxy + Podman setup of its own.

```
podman run -d --name technitium-docker-reconciler \
  -v /run/podman/podman.sock:/run/podman/podman.sock \
  -e DOCKER_HOST=unix:///run/podman/podman.sock \
  -e TECHNITIUM_HOST=10.0.0.9 \
  -e TARGET_IP=10.0.0.9 \
  -e TECHNITIUM_USER=... \
  -e TECHNITIUM_PASSWORD=... \
  ghcr.io/mfmseth/technitium-docker-reconciler:latest
```

Secrets (`TECHNITIUM_USER`/`TECHNITIUM_PASSWORD`) should come from an
untracked `.env` file populated from 1Password (vault `homelab`, item
`technitium`), not committed here -- same convention as everything else
in this homelab's secrets model.

## Relationship to `homelab2`'s `technitium_dns_record` role

That Ansible role still owns everything declared in `group_vars/all/
hosts.yml` -- `dns_records` and non-container `caddy_vhosts`. This tool
only ever manages records for containers carrying caddy-docker-proxy
labels; the two never compete because they're scoped to different record
sets (marker-tagged vs. untagged).
