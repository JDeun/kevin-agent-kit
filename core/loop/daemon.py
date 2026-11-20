from contextlib import contextmanager
import os
from pathlib import Path
import signal
import socket
import subprocess
from threading import Event
import time

from .config import ROOT, validate_evidence
from .engine import Engine
from .runtime import Busy, lock, module_command, private_dir
from .sandbox import probe
from .store import Store


@contextmanager
def address(state):
    fd = os.open(state, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield f"/proc/self/fd/{fd}/control.sock"
    finally:
        os.close(fd)


def contact(state, message):
    with address(state) as path, socket.socket(socket.AF_UNIX) as client:
        client.settimeout(1)
        client.connect(path)
        client.sendall(message + b"\n")
        with client.makefile("rb") as response:
            return response.readline(128).strip().decode("ascii")


def notify(state):
    if contact(state, b"stop") != "ok":
        raise ValueError("daemon did not acknowledge stop")


def stop(state):
    if not state.is_dir():
        return
    ensure_state(state)
    if not running(state):
        return
    try:
        notify(state)
    except OSError:
        if running(state):
            raise
        return
    deadline = time.monotonic() + 100
    while running(state):
        if time.monotonic() >= deadline:
            raise ValueError("daemon acknowledged stop and has not exited")
        time.sleep(0.05)


def running(state):
    if not state.is_dir():
        return False
    try:
        with lock(state / "daemon.lock"):
            return False
    except Busy:
        return True


def serve(cfg):
    state = cfg["state"]
    cancel = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: cancel.set())
    control = state / "control.sock"
    with (lock(state / "daemon.lock"), address(state) as path,
          socket.socket(socket.AF_UNIX) as server):
        if control.is_socket():
            control.unlink()
        server.bind(path)
        try:
            control.chmod(0o600)
            server.listen()
            server.settimeout(min(1, cfg["poll_seconds"]))
            next_tick = 0
            with Store(state, cfg["repo_id"], cfg["target"]).connect() as store, Engine(cfg, store, cancel) as engine:
                while not cancel.is_set():
                    if time.monotonic() >= next_tick:
                        engine.tick()
                        next_tick = time.monotonic() + cfg["poll_seconds"]
                    try:
                        client, _ = server.accept()
                    except TimeoutError:
                        continue
                    with client:
                        client.settimeout(0.5)
                        try:
                            with client.makefile("rb") as request:
                                operation = request.readline(32)
                            if operation == b"stop\n":
                                cancel.set()
                                client.sendall(b"ok\n")
                            elif operation == b"ping\n":
                                client.sendall(cfg["policy_id"].encode() + b"\n")
                        except (TimeoutError, ConnectionError):
                            continue
        finally:
            control.unlink(missing_ok=True)


def ensure_state(state):
    root = state.parent.parent
    private_dir(root)
    private_dir(state.parent)
    return private_dir(state)


def validate_execution(cfg):
    if not cfg["reviewer"] or not cfg["checks"]:
        raise ValueError("configure a reviewer and required checks in the trusted profile")
    probe()
    validate_evidence(cfg["evidence"], cfg["checks"])
    from .evidence import OPENSSL

    if not cfg["lens_reviewers"].get("security") or not cfg["review_observer"] or not cfg["review_key"]:
        raise ValueError("configure a dedicated security reviewer and an independent signed review observer")
    if not {"review-observer", "verify"} <= cfg["execution"]["sockets"].keys():
        raise ValueError("configure distinct broker sockets for review observation and required checks")
    if not os.access(OPENSSL, os.X_OK):
        raise ValueError("OpenSSL is required to verify signed evidence")


def start(cfg):
    state = cfg["state"]
    if running(state):
        if contact(state, b"ping") != cfg["policy_id"]:
            raise ValueError("daemon uses a different policy; stop and restart it")
        return
    validate_execution(cfg)
    args = module_command(ROOT, __package__, "--config", str(cfg["profile"]), "run")
    path = state / "daemon.log"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as log:
        child = subprocess.Popen(args, cwd="/", stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            if contact(state, b"ping") == cfg["policy_id"]:
                return
        except OSError:
            pass
        if child.poll() is not None:
            break
        time.sleep(0.05)
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=5)
    raise ValueError(f"daemon did not start, see {path}")
