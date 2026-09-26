#!/usr/bin/env python3
"""
technitium-docker-reconciler

Watches a Docker-API-compatible engine (Docker or Podman) for running
containers carrying Traefik router labels (`traefik.http.routers.<name>.rule`
with a `Host(...)` matcher), and reconciles a matching A record in
Technitium DNS for each declared hostname -- so a service only needs to be
declared once, as labels on its compose service, to get both reverse-proxy
routing (via Traefik) and a DNS record (via this tool).

Originally built against caddy-docker-proxy; switched to Traefik 2026-09-24
when the whole homelab moved off Caddy. Only the label-parsing layer
changed (this file's `hostnames_from_rule`/`desired_hostnames_from_docker`)
-- the Technitium reconciliation logic below is engine-agnostic and
untouched.

Same tag-and-prune model as the homelab-k8s Uptime Kuma reconciler: every
record this tool creates is marked with a `comments` value equal to
MANAGED_MARKER. On each pass it only ever adds/removes records carrying
that exact marker -- anything created by hand through the Technitium UI is
never touched, matched, or deleted.

Static records: hostnames that aren't a Traefik route (a bare SSH box, an
external CNAME, the zone's wildcard) can be declared in a YAML file named
by STATIC_RECORDS_FILE, so every record lives in git rather than only the
ones derived from labels. Each entry is `{name, type: A|CNAME, value,
ttl?}`. Static records are managed exactly like label-derived ones
(tagged, updated, pruned) and win over a label-derived record for the
same name.

Known limitation: record tagging depends on Technitium's `comments` field
(added in relatively recent Technitium releases). If your server version
doesn't return `comments` on `zones/records/get`, this tool will never be
able to identify records as "managed" and the prune step will simply never
delete anything -- it fails safe (leaves records alone), not unsafe. Verify
your Technitium version supports `comments` before relying on pruning.
"""

import os
import re
import sys
import time
import logging

import requests
import docker
import yaml

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("technitium-docker-reconciler")

MANAGED_MARKER = "managed-by=technitium-docker-reconciler"

TECHNITIUM_HOST = os.environ["TECHNITIUM_HOST"]
TECHNITIUM_PORT = os.environ.get("TECHNITIUM_PORT", "5380")
TECHNITIUM_USER = os.environ["TECHNITIUM_USER"]
TECHNITIUM_PASSWORD = os.environ["TECHNITIUM_PASSWORD"]
TARGET_IP = os.environ["TARGET_IP"]  # LAN IP of the Docker/Podman host these containers run on
DEFAULT_ZONE = os.environ.get("DEFAULT_ZONE", "mfmseth.com")
TTL = int(os.environ.get("TTL", "3600"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
STATIC_RECORDS_FILE = os.environ.get("STATIC_RECORDS_FILE", "")

MANAGED_TYPES = ("A", "CNAME")
# Field in a record's rData that holds its value, and the matching
# zones/records/{add,delete} form parameter, per record type.
RDATA_FIELD = {"A": "ipAddress", "CNAME": "cname"}

TECHNITIUM_BASE = f"http://{TECHNITIUM_HOST}:{TECHNITIUM_PORT}/api"

# Matches a Traefik router rule label, e.g.
# `traefik.http.routers.radarr.rule` -- never anything else
# (`traefik.http.routers.radarr.entrypoints`, `traefik.http.services...`,
# `traefik.http.middlewares...`, etc).
ROUTER_RULE_LABEL_RE = re.compile(r"^traefik\.http\.routers\.[^.]+\.rule$")

# Traefik v3 rule syntax: `Host(`a.com`)`, multiple args in one call
# (`Host(`a.com`,`b.com`)`, OR'd), and/or combined with && / || across
# several Host() calls. This pulls every backtick-quoted hostname out of
# every Host(...) call in the rule, regardless of how they're combined.
HOST_CALL_RE = re.compile(r"Host\(([^)]*)\)")
BACKTICK_ARG_RE = re.compile(r"`([^`]+)`")


def technitium_login():
    resp = requests.post(
        f"{TECHNITIUM_BASE}/user/login",
        data={"user": TECHNITIUM_USER, "pass": TECHNITIUM_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Technitium login failed: {data}")
    return data["token"]


def technitium_logout(token):
    try:
        requests.post(f"{TECHNITIUM_BASE}/user/logout", data={"token": token}, timeout=10)
    except requests.RequestException:
        pass


def ensure_zone(token, zone):
    resp = requests.post(
        f"{TECHNITIUM_BASE}/zones/create",
        data={"token": token, "zone": zone, "type": "Primary"},
        timeout=10,
    )
    data = resp.json()
    if data.get("status") != "ok" and "Zone already exists" not in data.get("errorMessage", ""):
        raise RuntimeError(f"Failed to ensure zone {zone}: {data}")


def get_managed_records(token, zone):
    resp = requests.get(
        f"{TECHNITIUM_BASE}/zones/records/get",
        params={"token": token, "domain": zone, "zone": zone, "listZone": "true"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to list records for zone {zone}: {data}")

    managed = {}
    for record in data["response"].get("records", []):
        if record.get("type") not in MANAGED_TYPES:
            continue
        if record.get("comments") != MANAGED_MARKER:
            continue
        managed[(record["name"].lower(), record["type"])] = record
    return managed


def add_record(token, domain, zone, rtype, value, ttl):
    if DRY_RUN:
        log.info("[dry-run] would add %s record %s -> %s", rtype, domain, value)
        return
    resp = requests.post(
        f"{TECHNITIUM_BASE}/zones/records/add",
        data={
            "token": token,
            "domain": domain,
            "zone": zone,
            "type": rtype,
            RDATA_FIELD[rtype]: value,
            "ttl": ttl,
            "overwrite": "true",
            "comments": MANAGED_MARKER,
        },
        timeout=10,
    )
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to add {rtype} record for {domain}: {data}")
    log.info("added/updated %s record %s -> %s", rtype, domain, value)


def delete_record(token, domain, zone, rtype, value):
    if DRY_RUN:
        log.info("[dry-run] would delete %s record %s (%s)", rtype, domain, value)
        return
    resp = requests.post(
        f"{TECHNITIUM_BASE}/zones/records/delete",
        data={
            "token": token,
            "domain": domain,
            "zone": zone,
            "type": rtype,
            RDATA_FIELD[rtype]: value,
        },
        timeout=10,
    )
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to delete {rtype} record for {domain}: {data}")
    log.info("deleted stale %s record %s", rtype, domain)


def hostnames_from_rule(rule_value):
    hosts = set()
    for call in HOST_CALL_RE.finditer(rule_value):
        for arg in BACKTICK_ARG_RE.finditer(call.group(1)):
            host = arg.group(1).strip()
            if host and "." in host:
                hosts.add(host)
    return hosts


def desired_hostnames_from_docker(client):
    desired = set()
    for container in client.containers.list(filters={"status": "running"}):
        labels = container.labels or {}
        # Matches Traefik's own exposedByDefault=false semantics -- a
        # container's routers only count if it opted in.
        if labels.get("traefik.enable", "").lower() != "true":
            continue
        for key, value in labels.items():
            if not ROUTER_RULE_LABEL_RE.match(key):
                continue
            desired |= hostnames_from_rule(value)
    return desired


def zone_for(hostname):
    # naive suffix match against DEFAULT_ZONE; extend here if you manage
    # more than one zone from this reconciler.
    if hostname == DEFAULT_ZONE or hostname.endswith("." + DEFAULT_ZONE):
        return DEFAULT_ZONE
    return None


def load_static_records(path):
    """Read STATIC_RECORDS_FILE into {(name, type): (value, ttl)}."""
    if not path:
        return {}
    with open(path) as f:
        entries = yaml.safe_load(f) or []
    records = {}
    for entry in entries:
        name = entry["name"].lower().rstrip(".")
        rtype = entry["type"].upper()
        if rtype not in MANAGED_TYPES:
            raise ValueError(f"{name}: unsupported record type {rtype} (supported: {MANAGED_TYPES})")
        records[(name, rtype)] = (str(entry["value"]).rstrip("."), int(entry.get("ttl", TTL)))
    return records


def desired_records(client):
    """Label-derived A records merged with static records; static wins."""
    desired = {(host.lower(), "A"): (TARGET_IP, TTL) for host in desired_hostnames_from_docker(client)}
    static = load_static_records(STATIC_RECORDS_FILE)
    static_names = {name for name, _ in static}
    # A name can't hold a CNAME alongside anything else, so a static entry
    # replaces every label-derived record for that name.
    desired = {key: val for key, val in desired.items() if key[0] not in static_names}
    desired.update(static)
    return desired


def reconcile_once(client):
    desired = desired_records(client)
    log.info("desired records: %s", ", ".join(f"{n} {t} {v}" for (n, t), (v, _) in sorted(desired.items())) or "(none)")

    token = technitium_login()
    try:
        ensure_zone(token, DEFAULT_ZONE)
        managed = get_managed_records(token, DEFAULT_ZONE)

        for (name, rtype), (value, ttl) in sorted(desired.items()):
            zone = zone_for(name)
            if zone is None:
                log.warning("skipping %s: not under managed zone %s", name, DEFAULT_ZONE)
                continue
            existing = managed.get((name, rtype))
            if (
                existing is None
                or existing.get("rData", {}).get(RDATA_FIELD[rtype], "").rstrip(".") != value
                or existing.get("ttl") != ttl
            ):
                add_record(token, name, zone, rtype, value, ttl)

        for (name, rtype), record in managed.items():
            if (name, rtype) not in desired:
                delete_record(token, name, DEFAULT_ZONE, rtype, record["rData"][RDATA_FIELD[rtype]])
    finally:
        technitium_logout(token)


def main():
    client = docker.from_env()
    once = "--once" in sys.argv

    while True:
        try:
            reconcile_once(client)
        except Exception:
            log.exception("reconcile pass failed")

        if once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
