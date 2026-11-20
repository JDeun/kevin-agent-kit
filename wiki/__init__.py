"""Repository knowledge wiki: immutable sources, synthesis pages and a map."""
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

DEFAULT_DIR = "docs/research"
STATUSES = ("current", "stale", "superseded")
REQUIRED = ("title", "status", "updated", "sources")
RESERVED = {"README.md", "AGENTS.md"}
MANIFEST = "manifest.json"
CONFLICT = re.compile(r"(?mi)^>\s*CONFLICT\b")
LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")

MAP_TEMPLATE = """# Research map

This directory is the repository's working knowledge. Read this map and the
relevant synthesis pages before starting research or design, and land what you
learn here. See [maintenance rules](AGENTS.md).

## Pages

<!-- One line per synthesis page: [Title](page.md) - what it answers. -->

## Sources

Raw material lives in [sources/](sources/). Sources are never edited.
"""

RULES_TEMPLATE = """# Wiki maintenance rules

- `sources/` holds raw material named `YYYY-MM-DD-slug.ext`. Add files through
  `wiki ingest`. Never edit or delete a source; add a newer one instead.
- Each synthesis page is `<slug>.md` with a front matter block:
  `title`, `status` (current, stale or superseded), `updated` (YYYY-MM-DD) and
  `sources` (a list of paths such as `sources/2026-01-31-notes.md`).
- Every claim in a page must be traceable to a listed source. Mark what you
  could not verify as an open question instead of stating it as fact.
- Link every page from the map in `README.md`.
- When new material contradicts a page, keep both statements and add a block
  beginning `> CONFLICT:` that names both sources. A person resolves it.
- Update `updated` whenever you change a page. Run `wiki lint` before commit.
"""


class WikiError(ValueError):
    pass


def locate(root, directory):
    """Return the absolute wiki directory, which must lie inside the checkout."""
    root = Path(root).resolve()
    relative = PurePosixPath(directory)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or str(relative) != directory.rstrip("/"):
        raise WikiError("the wiki directory must be a relative path inside the repository")
    path = root.joinpath(*relative.parts)
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise WikiError("the wiki directory cannot pass through a symlink")
    return path


def write_new(path, data):
    """Create a file exclusively. Return False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as stream:
        stream.write(data.encode("utf-8") if isinstance(data, str) else data)
    return True


def init(root, directory=DEFAULT_DIR):
    base = locate(root, directory)
    created, kept = [], []
    for relative, text in (("README.md", MAP_TEMPLATE), ("AGENTS.md", RULES_TEMPLATE),
                           (f"sources/{MANIFEST}", "{}\n")):
        (created if write_new(base / relative, text) else kept).append(f"{directory}/{relative}")
    return {"created": created, "kept": kept}


def sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_manifest(base):
    path = base / "sources" / MANIFEST
    if not path.is_file():
        raise WikiError("run `wiki init` first: the sources manifest is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WikiError("the sources manifest must be an object")
    return data


def ingest(root, source, directory=DEFAULT_DIR, *, today=None, name=None):
    """Copy raw material into sources/ with a date prefix and record its digest."""
    base = locate(root, directory)
    manifest = read_manifest(base)
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise WikiError("the source must be a regular file")
    stem = re.sub(r"[^a-z0-9]+", "-", (name or source.stem).lower()).strip("-")
    if not SLUG.fullmatch(stem):
        raise WikiError("could not derive a slug; pass --name")
    day = today or date.today().isoformat()
    try:
        date.fromisoformat(day)
    except ValueError:
        raise WikiError("the date must be YYYY-MM-DD") from None
    target = f"{day}-{stem}{source.suffix.lower()}"
    data = source.read_bytes()
    if not write_new(base / "sources" / target, data):
        raise WikiError(f"source already exists, sources are immutable: {target}")
    manifest[target] = hashlib.sha256(data).hexdigest()
    manifest_path = base / "sources" / MANIFEST
    with tempfile.NamedTemporaryFile("w", dir=manifest_path.parent, delete=False, encoding="utf-8") as stream:
        json.dump(dict(sorted(manifest.items())), stream, indent=2)
        stream.write("\n")
    os.replace(stream.name, manifest_path)
    return {"source": f"{directory}/sources/{target}", "sha256": manifest[target],
            "task": ingest_task(base, directory, target)}


def pages(base):
    return sorted(path for path in base.glob("*.md") if path.name not in RESERVED)


def front_matter(text):
    """Parse a small front matter subset: `key: value` and `- item` lists."""
    if not text.startswith("---\n"):
        return None, text
    head, separator, body = text[4:].partition("\n---\n")
    if not separator:
        return None, text
    data, key = {}, None
    for line in head.splitlines():
        if line.lstrip().startswith("- ") and key is not None and isinstance(data.get(key), list):
            data[key].append(line.lstrip()[2:].strip())
        elif ":" in line and not line.startswith((" ", "-")):
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            data[key] = [] if value == "" else value
    return data, body


def ingest_task(base, directory, target):
    existing = "\n".join(f"- {directory}/{path.name}" for path in pages(base)) or "- (no synthesis pages yet)"
    return f"""# Wiki ingest task

New raw source: `{directory}/sources/{target}`. Do not edit it.

1. Read `{directory}/README.md`, `{directory}/AGENTS.md` and the pages most
   related to this source.
2. Update each related synthesis page, or create one, so it reflects the
   source. Add `sources/{target}` to its `sources` list and set `updated`.
3. Update the map in `{directory}/README.md` so every page is linked.
4. If the source contradicts an existing statement, keep both and add a block
   beginning `> CONFLICT:` that names both sources. Do not resolve it yourself.
5. State only what the sources support. List unverified items as open questions.
6. Run `wiki lint --dir {directory}` and fix every error.

Existing pages:
{existing}
"""


def links_in(base, path, errors, *, text=None):
    """Check relative links and return the names of pages linked from the wiki directory."""
    text = path.read_text(encoding="utf-8") if text is None else text
    text = re.sub(r"(?s)```.*?```|<!--.*?-->", "", text)
    found = set()
    for target in LINK.findall(text):
        if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            errors.append(f"{path.name}: broken link `{target}`")
        elif resolved.parent == base.resolve():
            found.add(resolved.name)
    return found


def lint(root, directory=DEFAULT_DIR, *, strict=False):
    base = locate(root, directory)
    errors, warnings = [], []
    if not (base / "README.md").is_file():
        raise WikiError("the wiki map README.md is missing; run `wiki init`")
    manifest = read_manifest(base)
    sources = base / "sources"
    on_disk = {path.name for path in sources.iterdir() if path.is_file() and path.name != MANIFEST}
    for name, digest in manifest.items():
        if name not in on_disk:
            errors.append(f"sources/{name}: listed in the manifest but missing")
        elif sha256(sources / name) != digest:
            errors.append(f"sources/{name}: bytes changed after ingest")
    for name in sorted(on_disk - manifest.keys()):
        errors.append(f"sources/{name}: not recorded in the manifest, add it with `wiki ingest`")
    cited = set()
    page_set = pages(base)
    mapped = links_in(base, base / "README.md", errors)
    for page in page_set:
        meta, body = front_matter(page.read_text(encoding="utf-8"))
        label = page.name
        links_in(base, page, errors, text=body)
        if page.name not in mapped:
            errors.append(f"{label}: orphan page, not linked from README.md")
        if meta is None:
            errors.append(f"{label}: missing front matter")
            continue
        for key in REQUIRED:
            if key not in meta:
                errors.append(f"{label}: front matter lacks `{key}`")
        if "status" in meta and meta["status"] not in STATUSES:
            errors.append(f"{label}: status must be one of {', '.join(STATUSES)}")
        if "updated" in meta:
            try:
                date.fromisoformat(str(meta["updated"]))
            except ValueError:
                errors.append(f"{label}: updated must be YYYY-MM-DD")
        listed = meta.get("sources", [])
        if not isinstance(listed, list):
            errors.append(f"{label}: sources must be a list")
            listed = []
        if "sources" in meta and not listed and meta.get("status") == "current":
            errors.append(f"{label}: a current page must list at least one source")
        for item in listed:
            name = item.removeprefix("sources/")
            if not item.startswith("sources/") or "/" in name or name not in manifest:
                errors.append(f"{label}: source `{item}` is not an ingested file under sources/")
            else:
                cited.add(name)
        if CONFLICT.search(body):
            warnings.append(f"{label}: unresolved CONFLICT block")
    for name in sorted(set(manifest) - cited):
        warnings.append(f"sources/{name}: no synthesis page cites this source")
    if strict:
        errors, warnings = errors + warnings, []
    return {"errors": errors, "warnings": warnings, "pages": [page.name for page in page_set]}


def lint_task(root, directory=DEFAULT_DIR):
    base = locate(root, directory)
    listing = "\n".join(f"- {directory}/{path.name}" for path in pages(base)) or "- (none)"
    return f"""# Wiki lint task (judgement checks)

Deterministic checks are done by `wiki lint`. Review the pages below against
their cited sources. Report only what a source supports or contradicts.

For each page, read the page and every source in its front matter, then report:

- contradictions between pages, or between a page and a source;
- claims that newer sources have made stale (name the newer source);
- sentences with no support in any cited source;
- `> CONFLICT:` blocks that a later source now resolves.

Output a JSON array of objects with `page`, `kind` (contradiction, stale,
unsupported, resolved-conflict), `quote` and `evidence` (a source path or
`none`). Do not edit files. A person applies the fixes.

Pages:
{listing}
"""

