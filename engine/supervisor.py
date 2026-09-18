#!/usr/bin/env python3
"""
supervisor.py - PID 1 of an engine container.

The container lives across line start/stop and config changes. It shares one instance
directory (VOWIFI_INSTANCE_DIR, default /instance; $VOWIFI_DATA/instances/<id> on the control
plane) with the control plane, which drives it through two files in <instance dir>/run/:

  desired      "<running|stopped> <generation>"    written by the control plane. A new
                                                   generation restarts the session, so a
                                                   rewritten instance.json takes effect.
  engine.json  {"state": "running"|"stopped",       heartbeat written here every POLL_SECONDS;
                "generation": N, "ts": <epoch s>}   state says whether a session is currently
                                                   running. A stale heartbeat means this
                                                   container is gone.

A session is one run of session.sh (PIN keeper, SWu tunnel, Asterisk) in its own process
group. Its console output is copied to <instance dir>/logs/console.log with each line prefixed
by the epoch second, which lets the control plane read it like `docker logs --since`.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

INSTANCE_DIR = os.environ.get("VOWIFI_INSTANCE_DIR", "/instance")
RUN_DIR = os.path.join(INSTANCE_DIR, "run")
LOG_DIR = os.path.join(INSTANCE_DIR, "logs")
SESSION = "/usr/local/bin/session.sh"
POLL_SECONDS = 2
# Kept short enough that a SIGTERM to this container completes within Docker's default
# 10 s stop timeout (grace + one poll interval).
STOP_GRACE_SECONDS = 5

# The session scripts use the image's fixed paths; each is pointed at the shared directory.
IMAGE_PATHS = {
    "/config/instance.json": os.path.join(INSTANCE_DIR, "instance.json"),
    "/run/vowifi": RUN_DIR,
    "/logs": LOG_DIR,
    "/etc/asterisk/certificate.crt": os.path.join(INSTANCE_DIR, "tls", "certificate.crt"),
    "/etc/asterisk/certificate.key": os.path.join(INSTANCE_DIR, "tls", "certificate.key"),
}


def log(msg):
    print(f"[supervisor] {msg}", flush=True)


def link_image_paths():
    for link, target in IMAGE_PATHS.items():
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(target, link)


def read_desired():
    """(state, generation) from run/desired; missing or malformed means stopped."""
    try:
        with open(os.path.join(RUN_DIR, "desired")) as f:
            state, generation = f.read().split()
        return state, int(generation)
    except (OSError, ValueError):
        return "stopped", 0


def write_heartbeat(state, generation):
    path = os.path.join(RUN_DIR, "engine.json")
    with open(path + ".tmp", "w") as f:
        json.dump({"state": state, "generation": generation, "ts": int(time.time())}, f)
    os.replace(path + ".tmp", path)


class Session:
    """One run of session.sh in its own process group, so the PIN keeper, the tunnel loop
    and Asterisk can be stopped together."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [SESSION], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        threading.Thread(target=self._copy_console, daemon=True).start()

    def _copy_console(self):
        # Fresh console log per session, matching what a recreated container would show.
        with open(os.path.join(LOG_DIR, "console.log"), "wb") as console:
            for line in self.proc.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                console.write(b"%d %s" % (int(time.time()), line))
                console.flush()

    def alive(self):
        return self.proc.poll() is None

    def end(self):
        """Terminate the whole process group, forcefully after STOP_GRACE_SECONDS, and reap
        the children it re-parented to us (we are PID 1)."""
        pgid = self.proc.pid
        self._signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        self._signal_group(pgid, signal.SIGKILL)
        self.proc.wait()
        try:
            while os.waitpid(-1, os.WNOHANG) != (0, 0):
                pass
        except ChildProcessError:
            pass

    @staticmethod
    def _signal_group(pgid, sig):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass


def main():
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    link_image_paths()

    stopping = False

    def on_signal(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    session = None
    generation = 0
    log(f"instance dir {INSTANCE_DIR}")
    while not stopping:
        state, wanted = read_desired()
        if session and (state != "running" or wanted != generation or not session.alive()):
            log("ending session" if session.alive() else "session exited")
            session.end()
            session = None
        if session is None and state == "running":
            log(f"starting session (generation {wanted})")
            session = Session()
            generation = wanted
        write_heartbeat("running" if session else "stopped", generation)
        time.sleep(POLL_SECONDS)

    log("terminating")
    if session:
        session.end()
    write_heartbeat("stopped", generation)


if __name__ == "__main__":
    main()
