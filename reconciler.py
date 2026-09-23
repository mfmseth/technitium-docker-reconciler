#!/usr/bin/env python3
"""
technitium-docker-reconciler

Watches a Docker-API-compatible engine (Docker or Podman) for running
containers carrying caddy-docker-proxy site labels (`caddy` or `caddy_N`),
and reconciles a matching A record in Technitium DNS for each declared
hostname -- so a service only needs to be declared once, as labels on its
compose service, to get both reverse-proxy routing (via caddy-docker-proxy)
and a DNS record (via this tool).

Same tag-and-prune model as the homelab-k8s Uptime Kuma reconciler: every
record this tool creates is marked with a `comments` value equal to
MANAGED_MARKER. On each pass it only ever adds/removes records carrying
that exact marker -- anything created by hand through the Technitium UI is
never touched, matched, or deleted.

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
from urllib.parse import urlparse

import requests
import docker

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

TECHNITIUM_BASE = f"http://{TECHNITIUM_HOST}:{TECHNITIUM_PORT}/api"

# Matches only root caddy-docker-proxy site-block labels (`caddy`, `caddy_0`,
# `caddy_1`, ...) -- never nested directive labels like `caddy.reverse_proxy`
# or `caddy_0.reverse_proxy`.
SITE_LABEL_RE = re.compile(r"^caddy(_\d+)?$")


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
        if record.get("type") != "A":
            continue
        if record.get("comments") != MANAGED_MARKER:
            continue
        managed[record["name"]] = record
    return managed


def add_record(token, domain, zone):
    if DRY_RUN:
        log.info("[dry-run] would add A record %s -> %s", domain, TARGET_IP)
        return
    resp = requests.post(
        f"{TECHNITIUM_BASE}/zones/records/add",
        data={
            "token": token,
            "domain": domain,
            "zone": zone,
            "type": "A",
            "ipAddress": TARGET_IP,
            "ttl": TTL,
            "overwrite": "true",
            "comments": MANAGED_MARKER,
        },
        timeout=10,
    )
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to add record for {domain}: {data}")
    log.info("added/updated A record %s -> %s", domain, TARGET_IP)


def delete_record(token, domain, zone, ip_address):
    if DRY_RUN:
        log.info("[dry-run] would delete A record %s (%s)", domain, ip_address)
        return
    resp = requests.post(
        f"{TECHNITIUM_BASE}/zones/records/delete",
        data={
            "token": token,
            "domain": domain,
            "zone": zone,
            "type": "A",
            "ipAddress": ip_address,
        },
        timeout=10,
    )
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to delete record for {domain}: {data}")
    log.info("deleted stale A record %s", domain)


def extract_hostname(raw):
    raw = raw.strip()
    if not raw or raw.startswith(":") or raw == "localhost":
        return None
    candidate = raw if "//" in raw else f"//{raw}"
    host = urlparse(candidate).hostname
    if not host or "." not in host:
        return None
    return host


def desired_hostnames_from_docker(client):
    desired = set()
    for container in client.containers.list(filters={"status": "running"}):
        labels = container.labels or {}
        for key, value in labels.items():
            if not SITE_LABEL_RE.match(key):
                continue
            for piece in re.split(r"[,\s]+", value):
                host = extract_hostname(piece)
                if host:
                    desired.add(host)
    return desired


def zone_for(hostname):
    # naive suffix match against DEFAULT_ZONE; extend here if you manage
    # more than one zone from this reconciler.
    if hostname == DEFAULT_ZONE or hostname.endswith("." + DEFAULT_ZONE):
        return DEFAULT_ZONE
    return None


def reconcile_once(client):
    desired = desired_hostnames_from_docker(client)
    log.info("desired hostnames from caddy-docker-proxy labels: %s", sorted(desired) or "(none)")

    token = technitium_login()
    try:
        ensure_zone(token, DEFAULT_ZONE)
        managed = get_managed_records(token, DEFAULT_ZONE)

        for host in desired:
            zone = zone_for(host)
            if zone is None:
                log.warning("skipping %s: not under managed zone %s", host, DEFAULT_ZONE)
                continue
            existing = managed.get(host)
            if existing is None or existing.get("rData", {}).get("ipAddress") != TARGET_IP:
                add_record(token, host, zone)

        for host, record in managed.items():
            if host not in desired:
                delete_record(token, host, DEFAULT_ZONE, record["rData"]["ipAddress"])
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
