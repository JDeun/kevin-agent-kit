from pathlib import PurePosixPath
import re

from .git import Conflict

DESIGN_LENSES = ("architect", "sre")
TASK_KINDS = ("unclassified", "routine", "feature", "architecture", "research")
SECTIONS = (
    "canonical design", "failure modes and rollback", "latency budget", "memory budget", "test plan",
)


def document(git, sha, path):
    relative = PurePosixPath(path)
    if relative.suffix != ".md" or relative.is_absolute() or ".." in relative.parts or str(relative) != path:
        raise ValueError("provide a repository-relative Markdown design path")
    entry = git.run("ls-tree", "-z", "--full-tree", sha, "--", f":(literal){path}").stdout
    meta, _, filename = entry.rstrip("\0").partition("\t")
    mode, kind, blob = meta.split() if meta else ("", "", "")
    if filename != path or mode != "100644" or kind != "blob":
        raise ValueError("the design must be a committed regular Markdown file")
    return blob


def validate_document(git, blob):
    if int(git.run("cat-file", "-s", blob).stdout) > 64 * 1024:
        raise ValueError("design exceeds 64 KiB")
    source = git.run("cat-file", "blob", blob).stdout
    source.encode("utf-8")
    # Headings inside fenced code are examples, not sections.
    source = re.sub(r"(?ms)^[ ]{0,3}(`{3,}|~{3,}).*?^[ ]{0,3}\1[`~]*[ \t]*$", "", source)
    # Guidance placeholders left from the template do not count as content.
    source = re.sub(r"(?s)<!--.*?-->", "", source)
    sections = re.split(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", source)
    titles = [title.casefold() for title in sections[1::2]]
    repeated = sorted({title for title in titles if titles.count(title) > 1 and title in SECTIONS})
    if repeated:
        raise ValueError(f"design sections appear more than once: {', '.join(repeated)}")
    bodies = {title.casefold(): body.strip() for title, body in zip(sections[1::2], sections[2::2])}
    missing = [title for title in SECTIONS if not bodies.get(title)]
    if missing:
        raise ValueError(f"design sections need content: {', '.join(missing)}")


def approval_matches(git, approval, base, head, policy):
    if (approval is None or approval["status"] != "approved" or approval["policy"] != policy
            or approval["head"] == head):
        return False
    try:
        return (git.ancestor(approval["base"], base) and git.ancestor(approval["head"], head)
                and document(git, head, approval["path"]) == approval["blob"])
    except (Conflict, ValueError):
        return False


def wiki_landed(changed, directory):
    """True when a change set edits a wiki synthesis page, not only its map or sources."""
    prefix = directory + "/"
    for name in changed:
        rest = name.removeprefix(prefix) if name.startswith(prefix) else ""
        if rest.endswith(".md") and "/" not in rest and rest not in ("README.md", "AGENTS.md"):
            return True
    return False
