"""
engine.py - Per-SIM engine container lifecycle.

Each instance runs one `vowifi/engine` container that owns its ePDG tunnel + Asterisk. The
container lives across line start/stop: the manager drives it through files in the
instance directory it mounts (engine_static.py: instance.json, run/desired, the
run/engine.json heartbeat, logs/console.log) and reads the engine's runtime status files
from the same place, e.g. run/swu_status.json {state: CONNECTED} for tunnel state.

In Docker mode (this module) the manager also creates the container, with the right
mounts/caps/ports, and removes it when the line is deleted. In static mode
(VOWIFI_ENGINE_BACKEND=static) the containers are started by hand and the Docker-specific
functions are replaced at the end of this module.

PC/SC: engine containers are pcscd CLIENTS — they mount the HOST pcscd socket (/run/pcscd).
The pcsc-lite client library in the engine image is pinned to the SAME version as the host
pcscd (Dockerfile PCSC_VERSION == install.sh PCSC_VERSION) so client/server protocol matches.
"""
from __future__ import annotations

import json
import logging
import os

import docker

from . import config as cfg
from . import engine_static
from .engine_static import is_running, logs, stop, waiting_for_container  # noqa: F401 (re-exported)

log = logging.getLogger("vowifi.engine")

DATA_DIR = cfg.DATA_DIR
IMAGE = os.environ.get("VOWIFI_ENGINE_IMAGE", "vowifi/engine")
PCSCD_SOCK = os.environ.get("VOWIFI_PCSCD_DIR", "/run/pcscd")
# Absolute host path to the project data dir (needed for bind mounts when the manager
# itself runs in a container; defaults to DATA_DIR on the host).
HOST_DATA_DIR = os.environ.get("VOWIFI_HOST_DATA", DATA_DIR)
# Where the engine container mounts its instance directory.
INSTANCE_MOUNT = "/instance"


def _client():
    return docker.from_env()


def container_name(iid: str) -> str:
    return f"vowifi-engine-{iid}"


def start(inst: dict, settings: dict, dev_mounts: bool = False):
    """Publish the instance config, ask the supervisor for a (re)started session, and make
    sure the container is there and running."""
    iid = str(inst["id"])
    ports = inst.get("ports", {})
    engine_static.start(inst, settings)
    try:
        c = _client().containers.get(container_name(iid))
    except docker.errors.NotFound:
        c = None
    if c is not None and not _up_to_date(c, ports):
        log.info("recreating engine container %s (layout, image or ports changed)", c.name)
        c.remove(force=True)
        c = None
    if c is None:
        c = _create(iid, ports, dev_mounts)
        log.info("created engine container %s", c.name)
    elif c.status != "running":
        c.start()
        log.info("started engine container %s", c.name)
    return c.id


def _up_to_date(c, ports: dict) -> bool:
    """Whether an existing container can be kept: it must have the instance-directory mount
    (an older layout mounted instance.json as a single file, which never shows a rewrite),
    run the current image, and publish the wanted ports — all fixed at creation time."""
    mount_ok = any(m["Destination"] == INSTANCE_MOUNT for m in c.attrs["Mounts"])
    image_ok = c.attrs["Image"] == _client().images.get(IMAGE).id
    bound = {k: v[0]["HostPort"] for k, v in (c.attrs["HostConfig"]["PortBindings"] or {}).items()}
    ports_ok = bound == {k: str(v) for k, v in _port_bindings(ports).items()}
    return mount_ok and image_ok and ports_ok


def _port_bindings(ports: dict) -> dict:
    bindings = {
        "5060/udp": ports.get("sip_udp", 5060),
        "5061/tcp": ports.get("sip_tls", 5061),
        "8089/tcp": ports.get("webrtc", 8089),
        "5038/tcp": ports.get("ami", 5038),
    }
    # RTP range
    for p in range(ports.get("rtp_start", 10000), ports.get("rtp_start", 10000) + 60):
        bindings[f"{p}/udp"] = p
    return bindings


def _create(iid: str, ports: dict, dev_mounts: bool):
    volumes = {
        os.path.join(HOST_DATA_DIR, "instances", iid): {"bind": INSTANCE_MOUNT, "mode": "rw"},
        PCSCD_SOCK: {"bind": "/run/pcscd", "mode": "rw"},
    }
    if dev_mounts:
        eng = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "engine")
        for f in ["pin_keeper.py", "ami_usim.py", "render.py", "notify.py", "swu_ike.py",
                  "supervisor.py", "session.sh"]:
            volumes[os.path.join(eng, f)] = {"bind": f"/usr/local/bin/{f}", "mode": "ro"}
        volumes[os.path.join(eng, "templates")] = {"bind": "/opt/vowifi/templates", "mode": "ro"}

    return _client().containers.run(
        IMAGE,
        name=container_name(iid),
        detach=True,
        cap_add=["NET_ADMIN"],
        devices=["/dev/net/tun:/dev/net/tun:rwm"],
        volumes=volumes,
        ports=_port_bindings(ports),
        restart_policy={"Name": "unless-stopped"},
        environment={"VOWIFI_ID": iid},
        extra_hosts={"host.docker.internal": "host-gateway"},  # so notify.py can reach the manager
    )


def remove(iid: str):
    """Remove the container of a deleted line (stop() only idles it)."""
    try:
        _client().containers.get(container_name(iid)).remove(force=True)
    except docker.errors.NotFound:
        pass


def container_ip(iid: str) -> str | None:
    try:
        c = _client().containers.get(container_name(iid))
        nets = c.attrs["NetworkSettings"]["Networks"]
        for n in nets.values():
            if n.get("IPAddress"):
                return n["IPAddress"]
    except Exception:
        return None
    return None


def read_run_json(iid: str, name: str) -> dict | None:
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", name)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def read_pcscf(iid: str) -> str | None:
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", "pcscf")
    try:
        with open(path) as f:
            v = f.read().strip()
            return v or None
    except Exception:
        return None


def tunnel_installed(iid: str) -> bool:
    """True if the ims tunnel is up: the swu_ike daemon writes run/swu_status.json
    {state: CONNECTED} once the SWu (ePDG) IPsec tunnel is established."""
    st = read_run_json(iid, "swu_status.json")
    return st is not None and st.get("state") == "CONNECTED"


def charon_log(iid: str, tail: int = 200) -> str:
    """Recent SWu tunnel (IKE) log lines from the instance run dir. The file is named
    charon.log for control-plane/WebUI compatibility (the log-view key is 'charon')."""
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", "charon.log")
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-tail:])
    except Exception:
        return ""


def usim_status(iid: str) -> dict:
    return read_run_json(iid, "usim_status.json") or {}


# Static mode: the containers are started by hand and never touched from here.
if os.environ.get("VOWIFI_ENGINE_BACKEND") == "static":
    from .engine_static import start, remove, container_ip  # noqa: F811 (replaces the Docker versions)
