"""
engine_static.py - File-based engine lifecycle, shared with engine/supervisor.py.

Every engine container runs supervisor.py with the instance directory
$VOWIFI_DATA/instances/<id> mounted, and the control plane drives it through files in the
run/ subdirectory:

  desired      "<running|stopped> <generation>"   written here. A new generation restarts
                                                  the engine session, so a rewritten
                                                  instance.json takes effect.
  engine.json  {"state", "generation", "ts"}     heartbeat the supervisor rewrites every
                                                  few seconds; stale = no container serves
                                                  the line.

Console output is read from logs/console.log (lines prefixed with the epoch second).

In static mode (VOWIFI_ENGINE_BACKEND=static) this module is the whole backend: the
containers are started by hand (docker run, Compose, Kubernetes), never created or removed
here, and each engine's AMI host comes from VOWIFI_STATIC_ENGINES
("<id>=<host>,<id>=<host>,..."), defaulting to vowifi-engine-<id>. Docker mode (engine.py)
builds on the same functions and only adds creating and removing the container.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time

from . import config as cfg

log = logging.getLogger("vowifi.engine")

# The supervisor writes the heartbeat every 2 s, except while it spends up to ~7 s ending a
# session.
HEARTBEAT_MAX_AGE = 15


def _instance_dir(iid: str) -> str:
    return os.path.join(cfg.DATA_DIR, "instances", str(iid))


def _read_desired(iid: str) -> tuple[str, int]:
    try:
        with open(os.path.join(_instance_dir(iid), "run", "desired")) as f:
            state, generation = f.read().split()
        return state, int(generation)
    except (OSError, ValueError):
        return "stopped", 0


def _write_desired(iid: str, state: str):
    """Every write bumps the generation, so a start while running restarts the session."""
    run_dir = os.path.join(_instance_dir(iid), "run")
    os.makedirs(run_dir, exist_ok=True)
    generation = _read_desired(iid)[1] + 1
    path = os.path.join(run_dir, "desired")
    with open(path + ".tmp", "w") as f:
        f.write(f"{state} {generation}\n")
    os.replace(path + ".tmp", path)


def _heartbeat_fresh(iid: str) -> bool:
    try:
        with open(os.path.join(_instance_dir(iid), "run", "engine.json")) as f:
            heartbeat = json.load(f)
        return time.time() - float(heartbeat["ts"]) < HEARTBEAT_MAX_AGE
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _write_tls_files(iid: str, settings: dict):
    """Copy the certificate pair for the engine's SIP-TLS / WebRTC (WSS) listeners into the
    instance dir, where the supervisor links it to /etc/asterisk/certificate.{crt,key}. An
    explicit settings.tls pair wins; otherwise the control plane's own self-signed pair."""
    tls = settings.get("tls", {})
    crt, key = tls.get("cert_path"), tls.get("key_path")
    if not (crt and key and os.path.exists(crt) and os.path.exists(key)):
        crt = os.path.join(cfg.DATA_DIR, "certs", "self-signed.crt")
        key = os.path.join(cfg.DATA_DIR, "certs", "self-signed.key")
    tls_dir = os.path.join(_instance_dir(iid), "tls")
    os.makedirs(tls_dir, exist_ok=True)
    try:
        shutil.copyfile(crt, os.path.join(tls_dir, "certificate.crt"))
        shutil.copyfile(key, os.path.join(tls_dir, "certificate.key"))
    except OSError as e:
        log.warning("no TLS cert for engine %s WSS/8089 — browser softphone will not connect "
                    "until one exists (%s)", iid, e)


def start(inst: dict, settings: dict, dev_mounts: bool = False) -> str:
    """Publish the instance config and ask the supervisor for a (re)started session.
    dev_mounts is accepted for signature compatibility and ignored."""
    iid = str(inst["id"])
    cfg.write_instance_json(inst, settings)
    _write_tls_files(iid, settings)
    _write_desired(iid, "running")
    log.info("requested engine session start for instance %s", iid)
    return iid


def stop(iid: str) -> bool:
    was_running = is_running(iid)
    _write_desired(iid, "stopped")
    return was_running


def remove(iid: str):
    """Called when a line is deleted. The container was started by hand, so it is left to
    whoever started it; the supervisor idles once run/desired says stopped."""


def is_running(iid: str) -> bool:
    """True when the line is asked to run and a live supervisor is there to run it."""
    return _read_desired(iid)[0] == "running" and _heartbeat_fresh(iid)


def waiting_for_container(iid: str) -> bool:
    """True when the line is asked to run but no engine container serves it."""
    return _read_desired(iid)[0] == "running" and not _heartbeat_fresh(iid)


def container_ip(iid: str) -> str:
    """The engine's AMI host from VOWIFI_STATIC_ENGINES, or the vowifi-engine-<id> convention."""
    for entry in os.environ.get("VOWIFI_STATIC_ENGINES", "").split(","):
        key, _, host = entry.strip().partition("=")
        if key == str(iid) and host:
            return host
    return f"vowifi-engine-{iid}"


def logs(iid: str, tail: int = 200, since=None) -> str:
    """The last `tail` console lines, restricted to those logged at or after `since` (epoch
    seconds) when given."""
    try:
        with open(os.path.join(_instance_dir(iid), "logs", "console.log"), errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    out = []
    for line in lines:
        stamp, _, text = line.partition(" ")
        if since is None or (stamp.isdigit() and int(stamp) >= since):
            out.append(text)
    return "".join(out[-tail:])
