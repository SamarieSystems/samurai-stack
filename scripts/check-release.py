#!/usr/bin/env python3
"""Read-only, value-redacted source release hygiene audit using the stdlib.

This checks the candidate directory, not remote Git history or release assets.
It is a release gate for obvious leaks, not a certification that no secret
exists. See docs/MEDIABOT-AUDIT.md for coverage and synthetic fixture rules.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys


MAX_FILE_BYTES = 2 * 1024 * 1024
IGNORED_DIRECTORIES = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "node_modules",
})
EXAMPLE_SUFFIXES = (".example", ".sample", ".template")
PRIVATE_FILENAMES = frozenset({
    ".env", ".netrc", "credentials.json", "token.json", "tokens.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys",
})
PRIVATE_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".kdbx", ".db", ".sqlite", ".sqlite3",
    ".tar", ".tgz", ".zip", ".gz", ".7z", ".rar",
})
SYNTHETIC_MARKER = re.compile(r"(?:#|//)\s*release-audit:\s*synthetic\s*-\s*\S.{7,}", re.I)
SYNTHETIC_VALUE = re.compile(r"(?:fixture|synthetic|test)-[A-Za-z0-9_.:/-]+\Z", re.I)
REFERENCE_MARKER = re.compile(r"#\s*release-audit:\s*reference\s*-\s*\S.{7,}", re.I)
REFERENCE_VALUE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:token|password|key|username)\Z")
CREDENTIAL_NAME = re.compile(r"(?:^|_)(?:token|password|passwd|secret|api_key|private_key)(?:$|_)", re.I)
ENV_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
QUOTED_ASSIGNMENT = re.compile(
    r'''(?<![A-Za-z0-9_-])(?:["']?)([A-Za-z_][A-Za-z0-9_]*)(?:["']?)\s*[:=]\s*(["'])([^\r\n]*?)\2'''
)
PLACEHOLDER = re.compile(
    r"(?:change[-_]?me|replace[-_]?.*|your[-_].*|example[-_].*|placeholder|redacted|"
    r"not[-_]?set|disabled|none|null|<[^<>]+>)\Z", re.I
)
VARIABLE_REFERENCE = re.compile(r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*(?::[-?][^}\r\n]*)?\}|\$[A-Za-z_][A-Za-z0-9_]*|\{\{[^{}\r\n]+\}\})\Z")

TOKEN_RULES = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("discord-token", re.compile(
        r"(?<![A-Za-z0-9_-])(?:mfa\.[A-Za-z0-9_-]{20,}|"
        r"[A-Za-z0-9_-]{23,30}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{25,})(?![A-Za-z0-9_-])")),
    ("api-token", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b")),
    ("discord-webhook", re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?[.]com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}")),
    ("credential-url", re.compile(r'''https?://[^\s/:"']+:[^\s/@"']+@''')),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


@dataclass
class Report:
    files_scanned: int
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        return not self.findings


def is_example(path: Path) -> bool:
    return path.name.lower().endswith(EXAMPLE_SUFFIXES)


def is_private_filename(path: Path) -> bool:
    name = path.name.lower()
    if is_example(path):
        return False
    return (name in PRIVATE_FILENAMES or name.startswith(".env.")
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or any(part.lower() == "secrets" for part in path.parts[:-1]))


def is_synthetic_assignment(path: Path, line: str, value: str) -> bool:
    """Only obvious synthetic values in tests/fixtures can use an annotation."""
    fixture_location = any(part.lower() in {"tests", "fixtures"} for part in path.parts[:-1])
    return bool(fixture_location and SYNTHETIC_MARKER.search(line)
                and SYNTHETIC_VALUE.fullmatch(value))


def assignments(line: str):
    """Extract only simple literal assignments, without executing any input."""
    found = []
    for match in QUOTED_ASSIGNMENT.finditer(line):
        found.append((match.group(1), match.group(3)))
    match = ENV_ASSIGNMENT.match(line)
    if match:
        key, value = match.groups()
        if value.startswith(("'", '"')):
            # The quoted parser above handles inline comments and string bounds.
            pass
        else:
            value = value.split(" #", 1)[0].strip()
            # Python/shell expressions are not literal credential values.
            if not any(ch in value for ch in "()[]{}") or VARIABLE_REFERENCE.fullmatch(value):
                found.append((key, value))
    yield from dict.fromkeys(found)


def allowed_literal(path: Path, line: str, name: str, value: str) -> bool:
    if not value or VARIABLE_REFERENCE.fullmatch(value):
        return True
    if (name.upper().endswith(("_PATH", "_FILE", "_DIR", "_SECRETFILE"))
            and value.startswith(("/", "./", "../"))):
        return True
    if (path.suffix.lower() == ".py" and REFERENCE_MARKER.search(line)
            and REFERENCE_VALUE.fullmatch(value)):
        return True
    if PLACEHOLDER.fullmatch(value):
        return True
    return is_synthetic_assignment(path, line, value)


def python_literals(content: str):
    """Inspect literal Python assignments/dicts/arguments without evaluation.

    A string containing a negative test's example configuration is not itself
    an assignment to that credential. Real literal credential assignments are
    checked in every Python file, including tests.
    """
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = (target.id if isinstance(target, ast.Name) else
                        target.attr if isinstance(target, ast.Attribute) else
                        target.slice.value if isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str) else "")
                if name:
                    yield node.lineno, name, value.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    yield key.lineno, key.value, value.value
        elif isinstance(node, ast.keyword) and node.arg:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                yield node.lineno, node.arg, node.value.value


def load_deny_terms(path: Path) -> tuple[str, ...]:
    """Read operator-owned, private literal terms without echoing their values."""
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Private deny file cannot use symlinks.")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_size > 128 * 1024):
        raise ValueError("Private deny file must be a small regular file without hardlinks.")
    if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
        raise ValueError("Private deny file must be owned by this account and have mode 600.")
    terms = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(terms, list) or len(terms) > 1000
            or any(not isinstance(term, str) or not 1 <= len(term) <= 512
                   or term != term.strip()
                   or any(ord(character) < 32 or ord(character) == 127 for character in term)
                   for term in terms)):
        raise ValueError("Private deny file must contain a bounded JSON list of literal terms.")
    return tuple(dict.fromkeys(term.casefold() for term in terms))


def audit_text(path: Path, content: str, *, deny_terms=()) -> list[Finding]:
    """Return locations only; neither content nor matching values escape."""
    findings = set()
    lines = content.splitlines()
    parsed_literals = None
    if path.suffix.lower() == ".py":
        try:
            parsed_literals = list(python_literals(content))
        except (SyntaxError, RecursionError):
            # Invalid/incomplete source still gets the conservative text rules.
            pass
    for number, line in enumerate(lines, 1):
        for name, pattern in TOKEN_RULES:
            if pattern.search(line):
                # High-confidence token/private identifier rules cannot be
                # disabled by merely labeling a line as a test fixture.
                findings.add(Finding(path.as_posix(), number, name))
        if any(term.casefold() in line.casefold() for term in deny_terms):
            findings.add(Finding(path.as_posix(), number, "operator-denied-term"))
        if parsed_literals is not None or line.lstrip().startswith(("#", "//")):
            continue
        for name, value in assignments(line):
            if CREDENTIAL_NAME.search(name) and not allowed_literal(path, line, name, value):
                findings.add(Finding(path.as_posix(), number, "credential-literal"))
    for number, name, value in parsed_literals or []:
        if CREDENTIAL_NAME.search(name) and not allowed_literal(path, lines[number - 1], name, value):
            findings.add(Finding(path.as_posix(), number, "credential-literal"))
    return sorted(findings, key=lambda item: (item.path, item.line, item.rule))


def audit_tree(root: Path, *, deny_terms=()) -> Report:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Candidate must be an existing real directory, not a symlink.")
    root = root.resolve()
    findings = []
    scanned = 0

    def traversal_error(error):
        target = Path(error.filename) if error.filename else root
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            relative = "."
        findings.append(Finding(relative, 1, "unreadable-directory"))

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=traversal_error):
        folder = Path(directory)
        kept = []
        for name in sorted(dirs):
            if name in IGNORED_DIRECTORIES:
                continue
            target = folder / name
            if target.is_symlink():
                findings.append(Finding(target.relative_to(root).as_posix(), 1, "symlink"))
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            target = folder / name
            relative = target.relative_to(root)
            try:
                info = target.lstat()
                if stat.S_ISLNK(info.st_mode):
                    findings.append(Finding(relative.as_posix(), 1, "symlink"))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    findings.append(Finding(relative.as_posix(), 1, "non-regular-file"))
                    continue
                if is_private_filename(relative):
                    findings.append(Finding(relative.as_posix(), 1, "private-or-archive-filename"))
                if info.st_size > MAX_FILE_BYTES:
                    findings.append(Finding(relative.as_posix(), 1, "oversized-unscanned-file"))
                    continue
                raw = target.read_bytes()
                scanned += 1
                if b"\0" in raw:
                    findings.append(Finding(relative.as_posix(), 1, "binary-unscanned-file"))
                    continue
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeError:
                    findings.append(Finding(relative.as_posix(), 1, "non-utf8-unscanned-file"))
                    continue
                findings.extend(audit_text(relative, content, deny_terms=deny_terms))
            except OSError:
                findings.append(Finding(relative.as_posix(), 1, "unreadable-file"))
    return Report(scanned, sorted(set(findings), key=lambda item: (item.path, item.line, item.rule)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, help="Candidate source directory")
    parser.add_argument("--root", type=Path, help="Candidate source directory (alternative to positional path)")
    parser.add_argument("--deny-file", type=Path, help="Private JSON list of operator-specific literal terms, stored outside the candidate")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable, value-redacted report")
    args = parser.parse_args(argv)
    if args.path is not None and args.root is not None:
        parser.error("Use the positional path or --root, not both.")
    root = args.root or args.path or Path(__file__).resolve().parents[1]
    deny_terms = ()
    if args.deny_file is not None:
        try:
            if args.deny_file.resolve().is_relative_to(root.resolve()):
                raise ValueError("Private deny file cannot be shipped inside the candidate.")
            deny_terms = load_deny_terms(args.deny_file)
        except (OSError, ValueError):
            if args.json:
                print(json.dumps({"ok": False, "error": "invalid-private-deny-file"}))
            else:
                print("Private deny file must be a restricted, valid JSON list outside the candidate.", file=sys.stderr)
            return 2
    try:
        report = audit_tree(root, deny_terms=deny_terms)
    except (OSError, ValueError):
        if args.json:
            print(json.dumps({"ok": False, "error": "invalid-candidate-directory"}))
        else:
            print("Release audit requires an existing real candidate directory.", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": report.ok, "files_scanned": report.files_scanned,
                          "ignored_directories": sorted(IGNORED_DIRECTORIES),
                          "findings": [asdict(finding) for finding in report.findings]}, indent=2))
    else:
        for finding in report.findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}")
        print(f"Release audit {'passed' if report.ok else 'failed'}: "
              f"{report.files_scanned} files scanned, {len(report.findings)} findings.")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
