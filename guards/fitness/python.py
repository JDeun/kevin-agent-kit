"""Example AST checks for configured import boundaries and direct blocking calls."""

import ast
from collections import Counter
from pathlib import Path
import tokenize

from . import Violation, relative_path

if __package__ == "guards.fitness":
    from core.loop.review import matches
else:
    from ...core.loop.review import matches

DEFAULT_BLOCKING = (
    "builtins.open", "time.sleep", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.run", "urllib.request.urlopen",
)


def strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must be an array of nonempty strings")
    return value


def names(value, label):
    result = strings(value, label)
    if any(not all(part.isidentifier() for part in name.split(".")) for name in result):
        raise ValueError(f"{label} must contain dotted Python names")
    return result


class Check(ast.NodeVisitor):
    def __init__(self, path, package, forbidden, blocking):
        self.path = path
        self.package = package
        self.forbidden = forbidden
        self.blocking = blocking
        self.aliases = {"open": "builtins.open"}
        self.qualname = []
        self.in_async = False
        self.counts = Counter()

    def record_import(self, module):
        if any(module == name or module.startswith(name + ".") for name in self.forbidden):
            self.counts[Violation("forbidden-import", self.path, module)] += 1

    def visit_Import(self, node):
        for item in node.names:
            self.record_import(item.name)
            top = item.name.split(".")[0]
            self.aliases[item.asname or top] = item.name if item.asname else top

    def visit_ImportFrom(self, node):
        prefix = node.module.split(".") if node.module else []
        if node.level:
            if node.level > len(self.package):
                return
            prefix = self.package[:len(self.package) - node.level + 1] + prefix
        for item in node.names:
            module = ".".join([*prefix, item.name])
            self.record_import(module)
            if item.name != "*":
                self.aliases[item.asname or item.name] = module

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.aliases.pop(node.id, None)

    def scope(self, name, body, *, in_async=False, args=()):
        aliases, parent_async = self.aliases, self.in_async
        self.aliases = {key: value for key, value in aliases.items() if key not in args}
        self.in_async = in_async
        self.qualname.append(name)
        for statement in body:
            self.visit(statement)
        self.qualname.pop()
        self.aliases, self.in_async = aliases, parent_async

    def visit_FunctionDef(self, node):
        for part in [*node.decorator_list, node.args]:
            self.visit(part)
        if node.returns:
            self.visit(node.returns)
        parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                      node.args.vararg, node.args.kwarg]
        self.aliases.pop(node.name, None)
        self.scope(node.name, node.body, in_async=isinstance(node, ast.AsyncFunctionDef),
                   args={arg.arg for arg in parameters if arg})

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for part in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(part)
        self.aliases.pop(node.name, None)
        self.scope(node.name, node.body, in_async=self.in_async)

    def visit_Lambda(self, node):
        self.visit(node.args)  # Defaults are evaluated in the enclosing scope.

    def qualified_name(self, node):
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute) and (root := self.qualified_name(node.value)):
            return f"{root}.{node.attr}"
        return None

    def visit_Call(self, node):
        if self.in_async and (name := self.qualified_name(node.func)) in self.blocking:
            subject = f"{'.'.join(self.qualname)}:{name}"
            self.counts[Violation("async-blocking-call", self.path, subject)] += 1
        self.generic_visit(node)


def scan(root, settings):
    if not isinstance(settings, dict) or settings.keys() - {"source_root", "exclude", "forbidden_imports", "blocking_calls"}:
        raise ValueError("unsupported Python fitness settings")
    source_root = settings.get("source_root", ".")
    if not isinstance(source_root, str):
        raise ValueError("python.source_root must be a path")
    source = root if source_root == "." else root / Path(relative_path(source_root))
    if source.resolve() != source or not source.is_dir():
        raise ValueError("Python source root must be a real directory inside the checkout")
    excludes = strings(settings.get("exclude", []), "python.exclude")
    blocking = set(names(settings.get("blocking_calls", list(DEFAULT_BLOCKING)), "python.blocking_calls"))
    rules = settings.get("forbidden_imports", [])
    if not isinstance(rules, list):
        raise ValueError("python.forbidden_imports must be an array of tables")
    boundaries = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.keys() != {"paths", "modules"}:
            raise ValueError("forbidden import rules require paths and modules")
        boundaries.append((strings(rule["paths"], "paths"), names(rule["modules"], "modules")))

    counts = Counter()
    for file in sorted(source.rglob("*.py")):
        path = file.relative_to(root).as_posix()
        if any(matches(path, pattern) for pattern in excludes):
            continue
        if file.resolve() != file or not file.is_file():
            raise ValueError(f"expected a regular Python source file: {path}")
        forbidden = {module for paths, modules in boundaries if any(matches(path, pattern) for pattern in paths)
                     for module in modules}
        with tokenize.open(file) as stream:
            tree = ast.parse(stream.read(), filename=path)
        checker = Check(path, list(file.relative_to(source).parts[:-1]), forbidden, blocking)
        checker.visit(tree)
        counts.update(checker.counts)
    return counts
