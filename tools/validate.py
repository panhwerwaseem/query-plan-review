#!/usr/bin/env python3
"""Validate this repository as a portable Agent Skill and Claude Code plugin.

Checks the SKILL.md frontmatter against the open Agent Skills spec
(https://agentskills.io/specification), the plugin and marketplace manifests,
the eval file, and the "no network code in scripts" promise made in SECURITY.md.

Standard library only. Exit code 0 = valid, 1 = problems found.

Usage: python3 tools/validate.py [repo_root]
"""
import json
import py_compile
import re
import sys
from pathlib import Path

ALLOWED_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
RESERVED_WORDS = ("claude", "anthropic")
RESERVED_PLUGIN_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
REQUIRED_FILES = ("README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md")
NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(urllib\.request|urllib3|requests|httpx|aiohttp|socket|http\.client|ftplib|"
    r"smtplib|telnetlib|xmlrpc|websocket|websockets)\b",
    re.M,
)
PIPE_TO_SHELL_RE = re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b")
LINK_RE = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
CODE_PATH_RE = re.compile(r"`((?:scripts|references|assets)/[A-Za-z0-9_./-]+)`")


class Report:
    def __init__(self):
        self.errors = []

    def error(self, message):
        self.errors.append(message)


def split_key_value(line):
    match = re.match(r"^([A-Za-z0-9_-]+):(?:\s+(.*))?$", line)
    if not match:
        return None, None
    return match.group(1), (match.group(2) or "").strip()


def unquote(value, key, report):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if re.search(r":\s", value) or " #" in value or value[:1] in "[{>|*&!%@`":
        report.error(
            "frontmatter '%s' is unquoted but contains YAML-significant characters; "
            "wrap the value in quotes" % key
        )
    return value


def parse_frontmatter(text, report):
    """Parse the small YAML subset the spec needs: scalars plus one nested map."""
    if not text.startswith("---\n"):
        report.error("SKILL.md must start with a '---' frontmatter line")
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        report.error("SKILL.md frontmatter is not closed with '---'")
        return {}, text
    block = text[4:end]
    body = text[end + 4:].lstrip("\n")
    data = {}
    current_map = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[0] in " \t"
        key, value = split_key_value(raw.strip())
        if key is None:
            report.error("cannot parse frontmatter line: %r" % raw)
            continue
        if indented:
            if current_map is None:
                report.error("unexpected indented frontmatter line: %r" % raw)
                continue
            data[current_map][key] = unquote(value, key, report)
        elif value == "":
            data[key] = {}
            current_map = key
        else:
            data[key] = unquote(value, key, report)
            current_map = None
    return data, body


def check_skill(skill_dir, report):
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        report.error("missing %s" % skill_md)
        return None
    text = skill_md.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text, report)

    for key in meta:
        if key not in ALLOWED_KEYS:
            report.error(
                "frontmatter key '%s' is not in the portable spec (allowed: %s)"
                % (key, ", ".join(sorted(ALLOWED_KEYS)))
            )

    name = meta.get("name")
    if not isinstance(name, str) or not name:
        report.error("frontmatter 'name' is required")
    else:
        if len(name) > 64 or not NAME_RE.match(name):
            report.error("name %r must be 1-64 chars of a-z, 0-9 and single hyphens" % name)
        if name != skill_dir.name:
            report.error("name %r must match its folder name %r" % (name, skill_dir.name))
        for word in RESERVED_WORDS:
            if word in name:
                report.error("name must not contain the reserved word %r" % word)

    description = meta.get("description")
    if not isinstance(description, str) or not description:
        report.error("frontmatter 'description' is required")
    else:
        if len(description) > 1024:
            report.error("description is %d chars; the limit is 1024" % len(description))
        if "<" in description or ">" in description:
            report.error("description must not contain XML-style tags")

    compatibility = meta.get("compatibility")
    if isinstance(compatibility, str) and len(compatibility) > 500:
        report.error("compatibility is %d chars; the limit is 500" % len(compatibility))
    if "metadata" in meta and not isinstance(meta["metadata"], dict):
        report.error("metadata must be a map of string keys to string values")
    if "allowed-tools" in meta and not isinstance(meta["allowed-tools"], str):
        report.error("allowed-tools must be a space-separated string")

    line_count = len(body.splitlines())
    if line_count > 500:
        report.error("SKILL.md body is %d lines; keep it under 500" % line_count)

    referenced = set(LINK_RE.findall(body)) | set(CODE_PATH_RE.findall(body))
    for target in sorted(referenced):
        if re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
            continue
        if not (skill_dir / target).exists():
            report.error("SKILL.md references a missing file: %s" % target)

    if PIPE_TO_SHELL_RE.search(text):
        report.error("SKILL.md contains a download piped to a shell")
    return name


def check_scripts(skill_dir, report):
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return
    for path in sorted(scripts_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if PIPE_TO_SHELL_RE.search(source):
            report.error("%s pipes a download to a shell" % path)
        if path.suffix != ".py":
            continue
        try:
            py_compile.compile(str(path), doraise=True, cfile=None)
        except py_compile.PyCompileError as exc:
            report.error("%s does not compile: %s" % (path, exc.msg.strip()))
        match = NETWORK_IMPORT_RE.search(source)
        if match:
            report.error(
                "%s imports %s; bundled scripts must not make network calls"
                % (path, match.group(1))
            )


def check_evals(skill_dir, name, report):
    evals_path = skill_dir / "evals" / "evals.json"
    if not evals_path.is_file():
        report.error("missing %s" % evals_path)
        return
    try:
        data = json.loads(evals_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.error("%s is not valid JSON: %s" % (evals_path, exc))
        return
    if name and data.get("skill_name") != name:
        report.error("evals.json skill_name must be %r" % name)
    cases = data.get("evals")
    if not isinstance(cases, list) or len(cases) < 3:
        report.error("evals.json needs at least 3 eval cases")
        return
    for case in cases:
        label = "eval %r" % case.get("id")
        for field in ("prompt", "expected_output"):
            if not isinstance(case.get(field), str) or not case.get(field).strip():
                report.error("%s needs a non-empty %r" % (label, field))
        if not isinstance(case.get("assertions"), list) or not case.get("assertions"):
            report.error("%s needs a non-empty 'assertions' list" % label)
        for rel in case.get("files", []):
            if not (skill_dir / rel).exists():
                report.error("%s lists a missing file: %s" % (label, rel))


def load_json(path, report):
    if not path.is_file():
        report.error("missing %s" % path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.error("%s is not valid JSON: %s" % (path, exc))
        return None


def check_manifests(root, name, report):
    plugin = load_json(root / ".claude-plugin" / "plugin.json", report)
    if plugin is not None:
        plugin_name = plugin.get("name")
        if name and plugin_name != name:
            report.error("plugin.json name %r must equal the skill name %r" % (plugin_name, name))
        if isinstance(plugin_name, str) and plugin_name.startswith(RESERVED_PLUGIN_PREFIXES):
            report.error("plugin name %r uses a reserved prefix" % plugin_name)
        for field in ("description", "license", "repository"):
            if not plugin.get(field):
                report.error("plugin.json should set %r" % field)

    market = load_json(root / ".claude-plugin" / "marketplace.json", report)
    if market is not None:
        if not market.get("name"):
            report.error("marketplace.json needs a 'name'")
        if not isinstance(market.get("owner"), dict) or not market["owner"].get("name"):
            report.error("marketplace.json needs owner.name")
        entries = market.get("plugins")
        if not isinstance(entries, list) or len(entries) != 1:
            report.error("marketplace.json should list exactly one plugin")
        else:
            entry = entries[0]
            if name and entry.get("name") != name:
                report.error("marketplace plugin entry name must be %r" % name)
            if entry.get("source") != "./":
                report.error("marketplace plugin entry source must be './'")


def check_repo_files(root, report):
    for filename in REQUIRED_FILES:
        if not (root / filename).is_file():
            report.error("missing %s" % filename)
    readme = root / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        if "/plugin install" not in text:
            report.error("README.md needs a '/plugin install' line")
        if "npx skills add" not in text:
            report.error("README.md needs an 'npx skills add' line")
        if PIPE_TO_SHELL_RE.search(text):
            report.error("README.md contains a download piped to a shell")


def main(argv):
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    report = Report()
    skills_root = root / "skills"
    skill_dirs = sorted(p for p in skills_root.iterdir() if p.is_dir()) if skills_root.is_dir() else []
    if len(skill_dirs) != 1:
        report.error("expected exactly one folder in skills/, found %d" % len(skill_dirs))
        name = None
    else:
        skill_dir = skill_dirs[0]
        name = check_skill(skill_dir, report)
        check_scripts(skill_dir, report)
        check_evals(skill_dir, name, report)
    check_manifests(root, name, report)
    check_repo_files(root, report)

    if report.errors:
        print("INVALID: %d problem(s)" % len(report.errors))
        for message in report.errors:
            print("  - " + message)
        return 1
    print("OK: %s is a valid portable skill and plugin" % (name or root.name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
