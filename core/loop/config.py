import re
import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
MODULE_PREFIX = __package__.removesuffix("core.loop")
ROOT = PACKAGE_ROOT.parent if MODULE_PREFIX else PACKAGE_ROOT
NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


def project(root=ROOT):
    with (root / "project.toml").open("rb") as stream:
        metadata = tomllib.load(stream)["project"]
    if len(metadata["name"]) > 64 or not NAME.fullmatch(metadata["name"]):
        raise ValueError("project name must be a lowercase slug of up to 64 characters")
    return metadata


def config(path=None):
    with (ROOT / "project.toml").open("rb") as stream:
        data = tomllib.load(stream)["loop"]
    if path:
        with path.open("rb") as stream:
            data.update(tomllib.load(stream)["loop"])
    base = path.resolve().parent if path else Path.cwd()
    for key in ("repo", "state"):
        data[key] = (base / data[key]).resolve()
    for key in ("max_rounds", "max_jobs", "poll_seconds", "timeout_seconds",
                "retry_initial_seconds", "retry_limit", "retry_max_seconds"):
        if type(data[key]) is not int or data[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for role in ("reviewer", "worker", "verify"):
        command = data[role]
        if not isinstance(command, list) or any(not isinstance(x, str) or not x for x in command):
            raise ValueError(f"{role} must be an argv array")
        if role == "verify":
            continue
        order = data["models"]
        model = data[f"{role}_model"]
        ceiling = data["max_model"]
        if not command or model not in order or ceiling not in order:
            raise ValueError(f"{role} needs a command and a model listed in models")
        if len(set(order)) != len(order) or order.index(model) > order.index(ceiling):
            raise ValueError(f"{role} model exceeds max_model or the model order is ambiguous")
        data[role] = [model if item == "{model}" else item for item in command]
    for stage in ("review", "test"):
        limits = data["memory"].get(stage, {})
        for key in ("max_anon_mib", "min_available_mib"):
            limits.setdefault(key, 0)
            if type(limits[key]) is not int or limits[key] < 0:
                raise ValueError(f"memory.{stage}.{key} must be a nonnegative integer")
        data["memory"][stage] = limits
    return data
