"""Run approved commands with a private filesystem, process tree and network."""

import os
from pathlib import Path
import re
import stat

from .config import ROOT, strings
from .runtime import run

SYSTEM = ("/usr/bin", "/usr/lib", "/usr/lib64", "/usr/share", "/bin", "/sbin", "/lib", "/lib64")
SYSTEM_MOUNTS = tuple(path for path in SYSTEM if Path(path).is_dir())
PUBLIC_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ld.so.cache")
BWRAP = "/usr/bin/bwrap"
ROLES = ("reviewer", "worker", "persona")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TOOL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")


def environment(allowed, extra=None):
    env = {
        "PATH": "/tools:/usr/bin:/bin", "HOME": "/home/agent",
        "XDG_CONFIG_HOME": "/home/agent/.config", "XDG_STATE_HOME": "/home/agent/.local/state",
        "XDG_CACHE_HOME": "/home/agent/.cache", "XDG_DATA_HOME": "/home/agent/.local/share",
        "TMPDIR": "/tmp", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "PYTHONNOUSERSITE": "1",
    }
    for name in allowed:
        if (not NAME.fullmatch(name) or name in env or name in {"SSH_AUTH_SOCK", "BASH_ENV", "ENV", "CDPATH"}
                or name.startswith(("AGENT_", "LD_", "DYLD_", "PYTHON", "GIT_", "XDG_"))):
            raise ValueError(f"reserved or invalid environment name: {name}")
        if name in os.environ:
            env[name] = os.environ[name]
    env.update(extra or {})
    return env


def execution_policy(settings, forbidden=()):
    if not isinstance(settings, dict) or settings.keys() - {"tools", "env", "sockets"}:
        raise ValueError("execution settings accept tools, env and sockets")
    policy = {"tools": {}, "env": {}, "sockets": {}}
    if any(not isinstance(settings.get(section, {}), dict) for section in policy):
        raise ValueError("tools, env and sockets must be tables")
    for name, value in settings.get("tools", {}).items():
        if not TOOL_NAME.fullmatch(name):
            raise ValueError("invalid tool mount name")
        path = approved_path(value, forbidden, stat.S_ISREG)
        policy["tools"][name] = path
    for role, names in settings.get("env", {}).items():
        if role not in ROLES:
            raise ValueError("credential forwarding is limited to agent roles")
        policy["env"][role] = strings(names, role)
        environment(names)
    for role, value in settings.get("sockets", {}).items():
        if role not in ROLES:
            raise ValueError("model broker sockets are limited to agent roles")
        policy["sockets"][role] = approved_path(value, forbidden, stat.S_ISSOCK)
    return policy


def approved_path(value, forbidden, kind):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("sandbox mount source must be an absolute path")
    path = Path(value).resolve(strict=True)
    info = path.stat()
    if not kind(info.st_mode) or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
        raise ValueError(f"untrusted sandbox mount source: {path}")
    if any(path.is_relative_to(root) for root in forbidden):
        raise ValueError(f"sandbox mounts must be outside repository and state: {path}")
    return path


def command_line(argv, cwd, role, policy, extra=None, checks=None):
    if not argv:
        raise ValueError(f"configure the {role} command in the trusted profile")
    forward = environment(policy["env"].get(role, []), extra)
    cmd = [BWRAP, "--unshare-user", "--unshare-all", "--disable-userns", "--die-with-parent",
           "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
           "--dir", "/home", "--tmpfs", "/home/agent", "--dir", "/tools"]
    for path in SYSTEM_MOUNTS:
        cmd.extend(("--ro-bind", path, path))
    for path in PUBLIC_FILES:
        if Path(path).is_file():
            cmd.extend(("--ro-bind", path, path))
    cmd.extend(("--ro-bind", str(ROOT / "rules"), "/rules"))
    if cwd is not None:
        write = role in {"worker", "persona"}
        cmd.extend(("--bind" if write else "--ro-bind", str(cwd), "/workspace"))
        launch_dir = "/home/agent" if role in {"reviewer", "worker"} else "/workspace"
        cmd.extend(("--chdir", launch_dir))
        forward["AGENT_WORKSPACE"] = "/workspace"
        if write and (cwd / ".git").is_dir():
            cmd.extend(("--ro-bind", str(cwd / ".git"), "/workspace/.git"))
    if checks is not None:
        cmd.extend(("--ro-bind", str(checks), "/baseline"))
    for name, path in policy["tools"].items():
        cmd.extend(("--ro-bind", str(path), f"/tools/{name}"))
    if role in policy["sockets"]:
        cmd.extend(("--ro-bind", str(policy["sockets"][role]), "/run/model.sock"))
        forward["AGENT_MODEL_SOCKET"] = "/run/model.sock"
    # Execute from an allowlisted directory. Relative executables from candidate trees
    # must be invoked explicitly by a trusted wrapper.
    exe = argv[0]
    if "/" in exe and not any(
        exe.startswith(prefix) and "/" not in exe[len(prefix):] and exe[len(prefix):] not in {"", ".", ".."}
        for prefix in ("/tools/", "/usr/bin/", "/bin/")
    ):
        raise ValueError("sandbox executables must be in /tools, /usr/bin or /bin")
    return [*cmd, "--", *argv], forward


def redact(text, env_names):
    for name in env_names:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, "[redacted]")
    return text


def probe():
    import subprocess

    cmd, env = command_line(["/usr/bin/true"], None, "verify", execution_policy({}))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Bubblewrap isolation unavailable") from error
    if result.returncode:
        raise ValueError(f"Bubblewrap isolation unavailable: {result.stderr.strip()[:250]}")


def execute(argv, cwd, cancel, timeout, *, role, policy, stdin=None, extra=None, checks=None):
    cmd, env = command_line(argv, cwd, role, policy, extra, checks)
    return run(cmd, "/", cancel, timeout, prompt=stdin, env=env)
