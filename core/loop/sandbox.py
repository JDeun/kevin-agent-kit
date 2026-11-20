"""Run approved commands with a private filesystem, process tree and network."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat

from .config import ROOT, strings
from .paths import directory, identity, private_temporary_directory, regular_at, trusted
from .runtime import run

SYSTEM = ("/usr/bin", "/usr/lib", "/usr/lib64", "/usr/share", "/bin", "/sbin", "/lib", "/lib64")
SYSTEM_MOUNTS = tuple(path for path in SYSTEM if Path(path).is_dir())
PUBLIC_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ld.so.cache")
BWRAP = "/usr/bin/bwrap"
ROLES = ("reviewer", "worker", "persona")
EVIDENCE_ROLES = ("review-observer", "verify")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TOOL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")


@dataclass(frozen=True)
class Tool:
    path: Path
    inode: tuple
    digest: str
    executable: bool


def outside_path(value, forbidden):
    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("sandbox mount source must be an absolute path without traversal")
    path = Path(value)
    if any(path.is_relative_to(root) for root in forbidden):
        raise ValueError(f"sandbox mounts must be outside repository and state: {path}")
    return path


def read_tool(path, destination=None):
    digest = hashlib.sha256()
    with directory(path.parent) as parent, regular_at(parent, path.name, single_link=True) as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(2**20), b""):
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
        after = os.fstat(source.fileno())
        if (before.st_ctime_ns, before.st_mtime_ns) != (after.st_ctime_ns, after.st_mtime_ns):
            raise ValueError(f"sandbox tool changed while reading: {path}")
    return Tool(path, identity(before), digest.hexdigest(), bool(before.st_mode & 0o111))


def socket_identity(path):
    with directory(path.parent) as parent:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or not trusted(info):
            raise ValueError(f"untrusted sandbox mount source: {path}")
        return identity(info)


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
    ambient = (*(Path(path) for path in (*SYSTEM_MOUNTS, *PUBLIC_FILES)), ROOT / "rules")
    forbidden = (*forbidden, *ambient)
    if any(not isinstance(settings.get(section, {}), dict) for section in policy):
        raise ValueError("tools, env and sockets must be tables")
    for name, value in settings.get("tools", {}).items():
        if not TOOL_NAME.fullmatch(name):
            raise ValueError("invalid tool mount name")
        policy["tools"][name] = read_tool(outside_path(value, forbidden))
    for role, names in settings.get("env", {}).items():
        if role not in ROLES:
            raise ValueError("credential forwarding is limited to agent roles")
        policy["env"][role] = strings(names, role)
        environment(names)
    for role, value in settings.get("sockets", {}).items():
        if role not in (*ROLES, *EVIDENCE_ROLES):
            raise ValueError("broker socket must name a sandbox role")
        path = outside_path(value, forbidden)
        policy["sockets"][role] = (path, socket_identity(path))
    for role in EVIDENCE_ROLES:
        if role in policy["sockets"]:
            _, inode = policy["sockets"][role]
            if any(other != role and inode == registered for other, (_, registered) in policy["sockets"].items()):
                raise ValueError("evidence brokers need sockets separate from other roles")
    return policy


def command_tools(argv, policy):
    tools = policy["tools"]
    names = [part.removeprefix("/tools/") for part in argv if part.startswith("/tools/")]
    if argv and argv[0] in tools:
        names.insert(0, argv[0])
    selected = {}
    for name in names:
        if name not in tools:
            raise ValueError(f"unknown sandbox tool: {name}")
        selected[name] = tools[name]
    return selected


def command_line(argv, cwd, role, policy, extra=None, *, tools=None, bundle=None):
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
    if role in EVIDENCE_ROLES and cwd is not None:
        raise ValueError("evidence adapters cannot mount the candidate checkout")
    if cwd is not None:
        write = role in {"worker", "persona"}
        cmd.extend(("--bind" if write else "--ro-bind", str(cwd), "/workspace"))
        launch_dir = "/home/agent" if role in {"reviewer", "worker"} else "/workspace"
        cmd.extend(("--chdir", launch_dir))
        forward["AGENT_WORKSPACE"] = "/workspace"
        if write and (cwd / ".git").is_dir():
            cmd.extend(("--ro-bind", str(cwd / ".git"), "/workspace/.git"))
    if bundle is not None:
        if role not in EVIDENCE_ROLES:
            raise ValueError("only evidence adapters can mount an evidence bundle")
        cmd.extend(("--ro-bind", str(bundle), "/candidate.bundle"))
        forward["AGENT_CANDIDATE_BUNDLE"] = "/candidate.bundle"
    for name, path in (tools or {}).items():
        cmd.extend(("--ro-bind", str(path), f"/tools/{name}"))
    if role in policy["sockets"]:
        path, inode = policy["sockets"][role]
        if socket_identity(path) != inode:
            raise ValueError(f"sandbox broker changed: {path}")
        evidence = role in EVIDENCE_ROLES
        socket = "/run/evidence.sock" if evidence else "/run/model.sock"
        cmd.extend(("--ro-bind", str(path), socket))
        forward["AGENT_EVIDENCE_SOCKET" if evidence else "AGENT_MODEL_SOCKET"] = socket
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


def execute(argv, cwd, cancel, timeout, *, role, policy, stdin=None, extra=None, bundle=None):
    with private_temporary_directory("kit-tools-") as temp:
        paths = {}
        for name, tool in command_tools(argv, policy).items():
            path = temp / name
            with path.open("xb") as target:
                if read_tool(tool.path, target) != tool:
                    raise ValueError(f"sandbox tool changed: {tool.path}")
            path.chmod(0o500 if tool.executable else 0o400)
            paths[name] = path
        cmd, env = command_line(argv, cwd, role, policy, extra, tools=paths, bundle=bundle)
        return run(cmd, "/", cancel, timeout, prompt=stdin, env=env)
