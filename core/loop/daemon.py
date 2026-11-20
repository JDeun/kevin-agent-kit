import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Event
import time

from .engine import Engine
from .runtime import Busy, lock
from .store import Store


def listen_address(state):
    return str(state / "control.sock")


def notify(state):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(1)
        client.connect(listen_address(state))
        client.sendall(b"stop\n")


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
    state.mkdir(parents=True, exist_ok=True)
    cancel = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: cancel.set())
    address = Path(listen_address(state))
    with lock(state / "daemon.lock"), socket.socket(socket.AF_UNIX) as server:
        address.unlink(missing_ok=True)
        server.bind(str(address))
        try:
            address.chmod(0o600)
            server.listen()
            server.settimeout(min(1, cfg["poll_seconds"]))
            next_tick = 0
            with Store(state).connect() as store, Engine(cfg, store, cancel) as engine:
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
                                if request.readline(32) == b"stop\n":
                                    cancel.set()
                        except TimeoutError:
                            continue
        finally:
            address.unlink(missing_ok=True)


def start(cfg, config_path):
    state = cfg["state"]
    state.mkdir(parents=True, exist_ok=True)
    if running(state):
        return
    args = [sys.executable, "-P", "-m", "core.loop"]
    if config_path:
        args += ["--config", str(config_path.resolve())]
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    with (state / "daemon.log").open("ab") as log:
        child = subprocess.Popen(
            [*args, "run"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=env,
        )
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if Path(listen_address(state)).is_socket() and running(state):
            return
        if child.poll() is not None:
            break
        time.sleep(0.05)
    raise ValueError(f"daemon did not start, see {state / 'daemon.log'}")
