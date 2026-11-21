import hashlib
import os
from pathlib import Path
import re
import stat
import tomllib

from . import install
from .paths import directory, regular_at

NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


def metadata(raw):
    data = tomllib.loads(raw.decode("utf-8"))["project"]
    if len(data["name"]) > 64 or not NAME.fullmatch(data["name"]):
        raise ValueError("project name must be a lowercase slug of up to 64 characters")
    return data


def project():
    return metadata(install.project_bytes())


def xdg(variable, default):
    value = Path(os.environ.get(variable) or (Path.home() / default))
    if not value.is_absolute():
        return Path.home() / default
    return value


def state_root():
    return xdg("XDG_STATE_HOME", ".local/state") / project()["name"]


def profile_dir():
    return xdg("XDG_CONFIG_HOME", ".config") / project()["name"] / "profiles"


def repository(path):
    from .git import Git

    git = Git(path)
    root = Path(git.run("rev-parse", "--show-toplevel").stdout.rstrip("\n")).resolve()
    common = git.common()
    if common.name == ".git":
        root = common.parent
    return root, common, Git(root)


def fingerprint(*parts):
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def repo_id(common):
    info = common.stat()
    return fingerprint(os.fsencode(common), str((info.st_dev, info.st_ino)).encode())


def profile_name(repo, common):
    return f"{re.sub(r'[^a-zA-Z0-9_-]', '-', repo.name)[:48]}-{repo_id(common)[:16]}.toml"


def read_profile(path):
    from .git import Conflict, Git

    folder = profile_dir()
    path = Path(os.path.abspath(path))
    if path.parent != folder or path.suffix != ".toml":
        raise ValueError(f"executable settings must be in trusted profiles under {folder}")
    for entry in (folder.parent, folder, path):
        info = entry.lstat()
        kind = stat.S_ISREG if entry == path else stat.S_ISDIR
        if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"untrusted profile path: {entry}")
    try:
        Git(folder.resolve()).run("rev-parse", "--git-dir")
    except Conflict:
        pass
    else:
        raise ValueError("trusted profiles must be outside Git checkouts")
    with directory(folder, owner=os.getuid()) as parent, regular_at(parent, path.name, owner=os.getuid()) as stream:
        return path, stream.read()


def strings(value, key):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise ValueError(f"{key} must be an array of nonempty strings")
    return value


def validate_evidence(policy, checks):
    if (not isinstance(policy, dict) or set(policy) != {"observer", "checks"}
            or not isinstance(policy["checks"], dict) or policy["checks"].keys() != checks.keys()):
        raise ValueError("explicit evidence data approval is required for the observer and each check")
    for adapter in (policy["observer"], *policy["checks"].values()):
        if (not isinstance(adapter, dict) or set(adapter) != {"bundle", "retention_seconds"}
                or adapter["bundle"] not in ("incremental", "full")
                or type(adapter["retention_seconds"]) is not int or adapter["retention_seconds"] < 0):
            raise ValueError("evidence approval requires a bundle scope and nonnegative retention_seconds")


def rules_text(documents):
    return "\n".join(documents[name].decode("utf-8") for name in sorted(documents))


def policy_id(installed, profile, execution, keys):
    """Bind a policy to the approved install snapshot, the profile and its registered resources."""
    tools = [repr(tool).encode("utf-8", errors="surrogateescape") for tool in execution["tools"].values()]
    sockets = [repr(socket).encode("utf-8", errors="surrogateescape") for socket in execution["sockets"].values()]
    return fingerprint(installed.encode("ascii"), profile, *tools, *sockets, *keys)


def require_approval(approved):
    """Refuse a profile whose recorded install snapshot is not the one this process read."""
    installed = install.snapshot().digest
    if approved != installed:
        raise ValueError(
            f"this profile does not approve the installed kit (installed sha256={installed}, "
            f"approved {approved or 'none'}); review the install, then record it as install_sha256 "
            "(`init --approve-install` for a loop profile)")


def config(path=None, *, control_only=False):
    installed = install.snapshot()
    data = tomllib.loads(install.project_bytes().decode())["loop"]
    if path is None:
        repo, common, _ = repository(Path.cwd())
        path = profile_dir() / profile_name(repo, common)
    path, overrides = read_profile(path)
    settings = tomllib.loads(overrides.decode()).get("loop")
    if not isinstance(settings, dict):
        raise ValueError("invalid or unsupported [loop] profile settings")
    if not control_only:
        if "attestors" in settings:
            raise ValueError("replace legacy attestors with signed sources in check_keys and checks")
        if settings.keys() - data.keys():
            raise ValueError("invalid or unsupported [loop] profile settings")
    if not isinstance(settings.get("repo"), str) or not Path(settings["repo"]).is_absolute():
        raise ValueError("profile repo must be an absolute checkout path")
    data.update(settings)
    repo, common, git = repository(Path(data["repo"]))
    if path.resolve().is_relative_to(repo) or path.resolve().is_relative_to(common):
        raise ValueError("trusted profiles must be outside the checkout and Git directory")
    if not isinstance(data["target"], str):
        raise ValueError("target must be a branch name")
    git.ref(data["target"])
    identity = repo_id(common)
    state = state_root() / "projects" / fingerprint(identity.encode(), data["target"].encode())[:24]
    if state.resolve().is_relative_to(repo) or state.resolve().is_relative_to(common):
        raise ValueError("state must be outside the checkout and Git directory")
    if control_only:
        return {"state": state}
    require_approval(data["install_sha256"])
    if data["trust"] not in ("signed", "local"):
        raise ValueError('trust must be "signed" or "local"')
    if type(data["design_gate"]) is not bool:
        raise ValueError("design_gate must be a boolean")
    if type(data["repeat_class_rounds"]) is not int or data["repeat_class_rounds"] < 2:
        raise ValueError("repeat_class_rounds must be an integer of at least two")
    if type(data["wiki_landing"]) is not bool:
        raise ValueError("wiki_landing must be a boolean")
    wiki_dir = data["wiki_dir"]
    if (not isinstance(wiki_dir, str) or wiki_dir.startswith("/") or wiki_dir.endswith("/")
            or ".." in wiki_dir.split("/") or not wiki_dir):
        raise ValueError("wiki_dir must be a relative repository path")
    for key in ("max_rounds", "max_jobs", "poll_seconds", "timeout_seconds", "retry_initial_seconds",
                "retry_limit", "retry_max_seconds", "evidence_ttl_seconds"):
        if type(data[key]) is not int or data[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("reviewer", "review_observer", "worker", "repair_paths", "models"):
        strings(data[key], key)
    for key in ("checks", "check_commands", "lens_reviewers"):
        if not isinstance(data[key], dict):
            raise ValueError(f"{key} must be a table of named argv arrays")
        for name, command in data[key].items():
            if not NAME.fullmatch(name) or not strings(command, name):
                raise ValueError(f"invalid {key} name or command")
    local = data["trust"] == "local"
    if data["review_format"] not in ("receipt", "verdict"):
        raise ValueError("review_format must be receipt or verdict")
    if local and data["check_commands"]:
        raise ValueError("configure local commands under checks")
    if not local and data["checks"].keys() != data["check_commands"].keys():
        raise ValueError("each signed check needs a matching direct command in check_commands")
    if not local and (not isinstance(data["check_keys"], dict) or data["checks"].keys() != data["check_keys"].keys()):
        raise ValueError("each required check needs an approved public key")
    if local and (data["check_keys"] or data["review_key"] or data["review_observer"] or data["evidence"]):
        raise ValueError("trust = \"local\" does not use signing keys, a review observer or evidence approval")
    if data["evidence"] != {}:
        validate_evidence(data["evidence"], data["checks"])
    for role in ("reviewer", "worker"):
        order = data["models"]
        model, ceiling = data[f"{role}_model"], data["max_model"]
        if model not in order or ceiling not in order or len(set(order)) != len(order) or order.index(model) > order.index(ceiling):
            raise ValueError(f"{role} has an invalid model or model ceiling")
        data[role] = [model if item == "{model}" else item for item in data[role]]
    for lens, command in data["lens_reviewers"].items():
        if not install.has(f"reviews/lenses/{lens}.md"):
            raise ValueError("lens reviewer must name an installed lens")
        data["lens_reviewers"][lens] = [data["reviewer_model"] if item == "{model}" else item for item in command]
    for section, expected in (("memory", dict), ("lenses", dict)):
        if not isinstance(data[section], expected):
            raise ValueError(f"{section} must be a table")
    for name, patterns in data["lenses"].items():
        if not NAME.fullmatch(name) or not install.has(f"reviews/lenses/{name}.md"):
            raise ValueError("lens must name an installed review document")
        if name in ("general", "security", "architect", "sre"):
            raise ValueError(f"[loop.lenses] cannot select {name}: general and security always run on "
                             "implementation reviews, and architect and sre review designs only")
        strings(patterns, f"lenses.{name}")
    memory = {}
    for stage in ("review", "test"):
        limits = data["memory"].get(stage, {})
        if not isinstance(limits, dict):
            raise ValueError("memory stage limits must be a table")
        memory[stage] = {}
        for key in ("max_anon_mib", "min_available_mib"):
            value = limits.get(key, 0)
            if type(value) is not int or value < 0:
                raise ValueError(f"memory.{stage}.{key} must be a nonnegative integer")
            memory[stage][key] = value
    from .evidence import public_key
    from .sandbox import command_tools, execution_policy, outside_path

    forbidden = (repo, common, state.resolve())
    execution = execution_policy(data["execution"], forbidden=forbidden)
    rules = install.rules()
    execution["rules"] = rules
    review_key = b"" if data["review_key"] == "" else public_key(outside_path(data["review_key"], forbidden))
    check_keys = {name: public_key(outside_path(key, forbidden)) for name, key in data["check_keys"].items()}
    if review_key and review_key in check_keys.values():
        raise ValueError("review observation and check execution require separate signing keys")
    if "general" in data["lens_reviewers"]:
        raise ValueError("configure the general lens with reviewer")
    review_tools = []
    adapters = [data["reviewer"], *data["lens_reviewers"].values(), data["review_observer"]]
    for argv in adapters:
        if argv:
            tools = command_tools(argv, execution)
            tool = tools.get(argv[0].removeprefix("/tools/"))
            if not argv[0].startswith("/tools/") or not tool or not tool.executable:
                raise ValueError("review commands need an executable registered under /tools")
            if not local and any(tool.inode == other.inode or tool.digest == other.digest for other in review_tools):
                raise ValueError("each lens and the review observer need distinct review adapters")
            review_tools.append(tool)
    for argv in data["checks"].values():
        if local:
            continue
        tools = command_tools(argv, execution)
        tool = tools.get(argv[0].removeprefix("/tools/"))
        if not argv[0].startswith("/tools/") or not tool or not tool.executable:
            raise ValueError("check commands need an executable registered under /tools")
        if any(tool.inode == other.inode or tool.digest == other.digest for other in review_tools):
            raise ValueError("check and review adapters must be separate")
    for argv in (data["checks"] if local else data["check_commands"]).values():
        command_tools(argv, execution)
    command_tools(data["worker"], execution)
    data.update(
        repo=repo, state=state, repo_id=identity, profile=path, execution=execution, memory=memory,
        review_key=review_key, check_keys=check_keys,
        rules_text=rules_text(rules),
        policy_id=policy_id(installed.digest, overrides, execution, (review_key, *check_keys.values())),
    )
    return data
