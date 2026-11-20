"""Read-only lint of a hosted wiki (GitHub or GitLab `.wiki.git`). Never writes back."""
from pathlib import Path
import re
import subprocess
import tempfile

from . import LINK, WikiError

HOMES = ("home.md", "readme.md", "_sidebar.md")


def clone(url, destination):
    if url.startswith("-"):
        raise WikiError("the wiki URL cannot start with a dash")
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
         "clone", "--quiet", "--depth", "1", "--no-tags", "--", url, str(destination)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
        env={"PATH": "/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": str(destination.parent)},
    )
    if result.returncode:
        raise WikiError(f"could not read the wiki: {result.stderr.strip()[:200]}")


def audit(checkout):
    errors, warnings = [], []
    names = {path.relative_to(checkout).as_posix(): path for path in checkout.rglob("*")
             if path.is_file() and ".git" not in path.relative_to(checkout).parts}
    pages = {name for name in names if name.lower().endswith(".md")}
    linked = set()
    stems = {Path(name).with_suffix("").as_posix().lower(): name for name in pages}
    homes = [name for name in pages if name.lower() in HOMES]
    for name in sorted(pages):
        text = re.sub(r"(?s)```.*?```", "", names[name].read_text(encoding="utf-8", errors="replace"))
        for target in LINK.findall(text):
            if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            bare = target.split("#", 1)[0].strip("/")
            resolved = (Path(name).parent / bare).as_posix()
            found = next((candidate for candidate in (resolved, f"{resolved}.md", stems.get(resolved.lower()))
                          if candidate in names), None)
            if found is None:
                errors.append(f"{name}: broken link `{target}`")
            else:
                linked.add(found)
    if not homes:
        warnings.append("no home page found; cannot judge orphans")
    else:
        for name in sorted(pages - linked - set(homes)):
            warnings.append(f"{name}: not linked from any page")
    return {"errors": errors, "warnings": warnings, "pages": sorted(pages)}


def lint_remote(url):
    with tempfile.TemporaryDirectory(prefix="wiki-external-") as temp:
        checkout = Path(temp) / "wiki"
        clone(url, checkout)
        report = audit(checkout)
    report["read_only"] = True
    return report
