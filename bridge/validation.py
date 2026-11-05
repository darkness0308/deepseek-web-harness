"""Native static checks. Never import project modules or execute project code."""
import ast
from html.parser import HTMLParser
import json
import shutil
import subprocess
from urllib.parse import unquote, urlsplit


PLAIN_TEXT = {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log", ".ini", ".cfg", ".conf"}
PLAIN_NAMES = {".gitignore", ".gitattributes", ".editorconfig", "LICENSE", "README", "AGENTS.md", "CLAUDE.md"}


class HTMLChecks(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.references, self.errors, self.tags = set(), [], [], set()
        self.stack = []
        self.base = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.add(tag)
        if tag not in self.VOID:
            self.stack.append(tag)
        if tag == "base":
            self.base = True
        if attrs.get("id"):
            if attrs["id"] in self.ids:
                self.errors.append("Duplicate HTML id")
            self.ids.add(attrs["id"])
        if attrs.get("src"):
            self.references.append(attrs["src"])
        if tag in ("a", "link") and attrs.get("href"):
            self.references.append(attrs["href"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        # HTML permits omitted end tags (e.g. li/p); don't pretend to be an HTML
        # conformance validator. Track explicit document sections only.
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


def css_problems(text):
    """Structural CSS check: unterminated comments/strings and unbalanced brackets.

    This is not a CSS parser; it catches the truncated or mangled files that
    model-written stylesheets most often have.
    """
    problems, stack, i, line, n = [], [], 0, 1, len(text)
    pairs = {"}": "{", ")": "(", "]": "["}
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                problems.append(f"Unterminated comment starting on line {line}")
                return problems
            line += text.count("\n", i, end)
            i = end + 1
        elif ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n":
                    break
                j += 1
            if j >= n or text[j] != ch:
                problems.append(f"Unterminated string starting on line {line}")
                return problems
            i = j
        elif ch in "{([":
            stack.append((ch, line))
        elif ch in pairs:
            if not stack or stack[-1][0] != pairs[ch]:
                problems.append(f"Unmatched '{ch}' on line {line}")
                return problems
            stack.pop()
        i += 1
    if stack:
        problems.append(f"Unclosed '{stack[-1][0]}' opened on line {stack[-1][1]}")
    return problems


def javascript_problems(path):
    """Syntax-only check with ``node --check`` (parses, never executes)."""
    node = shutil.which("node")
    if not node:
        return None
    from .permissions import child_environment
    env = child_environment()
    def run(argv, data=None):
        return subprocess.run(argv, input=data, capture_output=True, timeout=20, cwd=path.parent, env=env)
    try:
        result = run([node, "--check", str(path)])
        text = result.stderr.decode("utf-8", "replace")
        if result.returncode and ("import statement" in text or "export" in text):
            # Older Node treats .js as CommonJS; retry the source as an ES module.
            result = run([node, "--input-type=module", "--check"], path.read_bytes())
            text = result.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ["Node.js syntax check could not run"]
    if result.returncode == 0:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    detail = next((l for l in lines if "Error" in l), lines[0] if lines else "syntax error")
    location = next((l for l in lines if l.rsplit(":", 1)[-1].strip().isdigit()), "")
    line = location.rsplit(":", 1)[-1].strip() if location else ""
    return [f"Invalid JavaScript syntax{' at line ' + line if line else ''}: {detail[:200]}"]


def validate_files(registry, paths, control):
    results = []
    for value in paths:
        control.check()
        path = registry.resolve(value, control)
        coverage, errors, warnings = "", [], []
        if not path.is_file() or path.stat().st_size > registry_max_bytes():
            raise ValueError("Validation requires regular files within HARNESS_MAX_FILE_BYTES")
        text = path.read_bytes().decode("utf-8")
        suffix = path.suffix.lower()
        if suffix == ".py":
            coverage = "Python syntax only; no import, execution or behavioral tests"
            try:
                ast.parse(text, filename=value)
            except SyntaxError as exc:
                errors.append(f"Invalid Python syntax at line {exc.lineno}, column {exc.offset}")
        elif suffix == ".json":
            coverage = "JSON syntax only; no application-schema validation"
            try:
                json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, RecursionError):
                errors.append("Invalid JSON syntax")
        elif suffix in (".html", ".htm"):
            coverage = "HTML document sections, duplicate IDs, local fragment targets and referenced-file existence; no rendering or JS/CSS syntax"
            parser = HTMLChecks()
            parser.feed(text); parser.close()
            errors.extend(parser.errors)
            for section in ("html", "head", "body", "title"):
                if section not in parser.tags:
                    warnings.append("HTML document section omitted: " + section)
            if any(tag in parser.stack for tag in ("html", "head", "body", "title")):
                errors.append("Unclosed explicit HTML document section")
            if parser.base:
                errors.append("HTML base URLs need a browser check; local references cannot be established here")
            else:
                for reference in parser.references:
                    control.check()
                    try:
                        url = urlsplit(reference)
                        if url.scheme or url.netloc:
                            continue
                        if not url.path:
                            if url.fragment and unquote(url.fragment) not in parser.ids:
                                errors.append("Local fragment target is missing")
                            continue
                        # Root-relative URLs are relative to the session's serving
                        # root; document-relative URLs remain relative to the file.
                        base = registry.path(getattr(control, "cwd", ".")) if url.path.startswith("/") else path.parent
                        target = registry.path(str(base / unquote(url.path).lstrip("/")))
                        if not target.exists():
                            errors.append("Referenced local resource does not exist")
                    except (ValueError, OSError):
                        errors.append("Referenced local resource is outside workspace or invalid")
        elif suffix == ".css":
            coverage = "CSS structure only (balanced blocks, closed comments and strings); not property or selector validity"
            errors.extend(css_problems(text))
        elif suffix in (".js", ".mjs", ".cjs"):
            found = javascript_problems(path)
            if found is None:
                coverage = "Unsupported file type (JavaScript syntax needs Node.js, which is not installed)"
                errors.append("JavaScript syntax validation needs Node.js on PATH; use project commands if available")
            else:
                coverage = "JavaScript syntax only (node --check parses without executing); no linting or behavior"
                errors.extend(found)
        elif suffix in (".yaml", ".yml"):
            coverage = "YAML syntax only; no application-schema validation"
            try:
                import yaml
                list(yaml.safe_load_all(text))
            except Exception as exc:  # noqa: BLE001 - parser error classes vary
                mark = getattr(exc, "problem_mark", None)
                errors.append("Invalid YAML syntax" + (f" at line {mark.line + 1}" if mark else ""))
        elif suffix == ".toml":
            try:
                import tomllib
            except ImportError:  # Python 3.10 has no tomllib; tomli has the same API
                try:
                    import tomli as tomllib
                except ImportError:
                    tomllib = None
            if tomllib is None:
                coverage = "Unsupported file type (TOML syntax needs Python 3.11+ or the tomli package)"
                errors.append("TOML syntax validation is unavailable on this Python; use project commands if available")
            else:
                coverage = "TOML syntax only; no application-schema validation"
                try:
                    tomllib.loads(text)
                except Exception as exc:  # noqa: BLE001
                    errors.append("Invalid TOML syntax: " + str(exc)[:120])
        elif suffix in PLAIN_TEXT or path.name in PLAIN_NAMES:
            # Nothing to parse, so say exactly what was established: the file
            # exists and is valid UTF-8. This is not a syntax or behavior check.
            coverage = "Plain text: file exists and decodes as UTF-8; this type has no syntax to validate"
            if "\x00" in text:
                errors.append("File contains NUL bytes and is not plain text")
        else:
            coverage = "Unsupported file type"
            errors.append("Native syntax validation supports .py, .json, .html/.htm, .css, .js/.mjs/.cjs (with Node.js), "
                          ".yaml/.yml, .toml and plain text (.txt, .md, .csv ...); use project commands for other types")
        results.append({"name": "native validation: " + registry.show(path, control),
                        "path": registry.show(path, control), "exit_code": 1 if errors else 0,
                        "coverage": coverage, "errors": errors[:50], "warnings": warnings[:50]})
    return results


def registry_max_bytes():
    from . import config
    return config.MAX_FILE_BYTES
