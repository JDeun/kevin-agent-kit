import ast
import re
from pathlib import Path


SKIP_DIRS = {".git", ".hg", ".svn", ".venv", ".pytest_cache", "__pycache__", "node_modules"}
MAX_BYTES = 2 * 1024 * 1024

TEXT_RULES = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "token-pattern": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
        r"AKIA[A-Z0-9]{16}|sk-(?:proj-)?[A-Za-z0-9_-]{24,})\b"
    ),
    "secret-assignment": re.compile(
        r"""(?:^|[ {,;])["']?(?:[\w.-]*(?:api[_-]?key|access[_-]?token|secret|password))["']?"""
        r"""\s*[:=]\s*(["']?)([A-Za-z0-9][A-Za-z0-9_.+/@=-]{7,})\1(?=[\s,;}]|$)""",
        re.IGNORECASE,
    ),
    "remote-shell": re.compile(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:[\w./-]*/)?(?:ba|da|z|k)?sh\b"),
    "destructive-command": re.compile(
        r"""\b(?:rm\s+(?:-\w+\s+)*-[a-z]*[rR][a-z]*\s+(?:-\w+\s+)*(?:--\s+)?"""
        r"""["']?(?:/|~|\$HOME)(?=[/*"'\s;]|$)|"""
        r"git\s+(?:reset\s+--hard|clean\s+-[a-z]*f)|chmod\s+(?:-R\s+)?777\b)"
    ),
    "broad-scope": re.compile(
        r"""["']?(?:allowed[_-]?paths|approved[_-]?commands|write[_-]?scope|filesystem[_-]?scope)["']?"""
        r"""\s*[:=]\s*(?:\[[^\]\n]*?)?["'](?:/|/\*|\*\*?)["']""",
        re.IGNORECASE,
    ),
}

TEMPLATE_PROMPT = re.compile(
    r"""\b(?:[A-Za-z_]*prompt|content)\s*[:=]\s*[^\n]*?"""
    r"""(?:\$\{|\{\{)\s*(?:untrusted\w*|user_?input|request_?body|tool_?output)\b""",
    re.IGNORECASE,
)
PLACEHOLDER = re.compile(r"^(?:example$|sample$|placeholder$|your[-_]|changeme$|dummy$|redacted$|none$)", re.I)

SOURCES = {"input", "read_text", "read_bytes", "request_body", "recv", "tool_output", "user_input"}
SINKS = {"ask", "send_prompt", "generate_prompt"}
UNTRUSTED_NAME = re.compile(r"^(?:untrusted\w*|user_?input|request_?body|tool_?output)$", re.I)


def expression_name(expression: ast.AST) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    return None


def contains_untrusted(expression: ast.AST, names: set[str]) -> bool:
    for part in ast.walk(expression):
        name = expression_name(part)
        if name and (name in names or UNTRUSTED_NAME.fullmatch(name)):
            return True
        if isinstance(part, ast.Call) and expression_name(part.func) in SOURCES:
            return True
    return False


def prompt_boundaries(content: str) -> set[int]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return set()
    nodes = list(ast.walk(tree))
    bindings = [(node.targets, node.value) for node in nodes if isinstance(node, ast.Assign)]
    bindings.extend(
        ([node.target], node.value)
        for node in nodes if isinstance(node, ast.AnnAssign) and node.value is not None
    )

    names = set()
    while True:
        new_names = {
            name
            for targets, value in bindings if contains_untrusted(value, names)
            for target in targets if (name := expression_name(target))
        }
        if new_names <= names:
            break
        names.update(new_names)

    tainted = {
        target.lineno
        for targets, value in bindings if contains_untrusted(value, names)
        for target in targets if (expression_name(target) or "").lower().endswith("prompt")
    }
    for node in nodes:
        if isinstance(node, ast.Call):
            if expression_name(node.func) in SINKS and any(
                contains_untrusted(arg, names) for arg in node.args
            ):
                tainted.add(node.lineno)
            for keyword in node.keywords:
                if keyword.arg in {"prompt", "system_prompt", "messages"} and contains_untrusted(keyword.value, names):
                    tainted.add(keyword.lineno)
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant)}
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and (
                    key.value == "prompt" or key.value == "content" and "role" in keys
                ) and contains_untrusted(value, names):
                    tainted.add(value.lineno)
    return tainted


def findings_for(path: Path, root: Path) -> list[dict]:
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or b"\0" in data:
        return []
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return []

    findings = set()
    relative = path.relative_to(root).as_posix()
    for line_number, line in enumerate(content.splitlines(), 1):
        for rule, pattern in TEXT_RULES.items():
            if any(
                rule != "secret-assignment" or not PLACEHOLDER.match(match[2])
                for match in pattern.finditer(line)
            ):
                findings.add((line_number, rule))
        if TEMPLATE_PROMPT.search(line):
            findings.add((line_number, "untrusted-prompt"))
    if path.suffix == ".py":
        findings.update((number, "untrusted-prompt") for number in prompt_boundaries(content))
    return [
        {"rule": rule, "path": relative, "line": number}
        for number, rule in sorted(findings)
    ]


def scan(root: Path, paths: list[Path]) -> list[dict]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Scan root must be a directory")
    files = set()
    for requested in paths or [Path(".")]:
        try:
            path = (root / requested).resolve()
        except RuntimeError as error:
            raise ValueError("A requested scan path contains a symlink loop") from error
        if not path.is_relative_to(root):
            raise ValueError("A requested path escapes the scan root")
        if not path.exists():
            raise ValueError("A requested scan path does not exist")
        pending = [path]
        while pending:
            current = pending.pop()
            if current.is_symlink():
                continue
            if current.is_dir() and current.name not in SKIP_DIRS:
                pending.extend(current.iterdir())
            elif current.is_file():
                files.add(current)

    return [finding for path in sorted(files) for finding in findings_for(path, root)]
