#!/usr/bin/env python3
"""Install a managed system-role entrypoint for DeepSeek Harness (dsh).

The installer leaves the harness checkout (and any pnpm-installed profile)
untouched. It manages files under the Harness home (~/.dsh by default):

  ~/.dsh/cordis.patch.yml        home-level user patch layer (dsh already reads it)
  ~/.dsh/keysmith/system-role.md managed system prompt body
  ~/.dsh/keysmith/plugin.cjs     a small Cordis plugin that registers the
                                 system-role.md as a system-prompt section
  ~/.dsh/keysmith/config.json    install manifest (paths, state, hashes)

DeepSeek Harness composes its prompt tree from Cordis patch layers. The
home-level layer is applied to every profile after the bundle layers, so a
patch row inserted there reaches every `dsh web` / `dsh --profile ...` boot.
The inserted row mounts the plugin.cjs (a relative local module beside the
patch file) with a `!!js` expression that injects the absolute managed
system-role.md path at boot. The plugin registers the file's contents as
`managed:keysmith` in the system-prompt registry (order 5 — after the persona,
before tool guidance), so the text rides the harness's true system message
pipeline (ctx.systemPrompt -> renderPrompt -> request.system), not the
user-role AGENTS.md channel.

The installer only writes the Harness home and backs up every file it would
touch. It never reads, stores, or prints model API keys or conversation data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_SYSTEM_FILE = REPO_ROOT / "system-role.md"

# Harness home: $DSH_HOME, else ~/.dsh (mirrors @deepseek-ai/dsh-home-paths).
DEFAULT_DSH_HOME_ENV = "DSH_HOME"
DEFAULT_DSH_HOME_DIR_NAME = ".dsh"

DEFAULT_MANAGED_SUBDIR = "keysmith"
DEFAULT_SYSTEM_FILE_NAME = "system-role.md"
DEFAULT_PLUGIN_NAME = "plugin.cjs"
DEFAULT_CONFIG_FILE_NAME = "config.json"

# dsh home-level user patch layer, read by every profile boot.
DEFAULT_HOME_PATCH_NAME = "cordis.patch.yml"

# Row identity in the home patch layer. Keep stable across installs so a
# reinstall patches the same row instead of appending duplicates.
PATCH_ROW_ID = "keysmith"
# Plugin-internal Cordis plugin name (exports.name of plugin.cjs).
PATCH_PLUGIN_NAME = "keysmith"

# Ordered system-prompt section slot: persona is 0, tool guidance is 100+.
PERSONA_ORDER = 0
TOOL_GUIDANCE_ORDER = 100
SYSTEM_ROLE_ORDER = 5

# GLM ChatML export markers stripped when the source prompt came from an export.
IM_START_SYSTEM = "<|im_start|>system:"
IM_END = "<|im_end|>"


class KeysmithError(Exception):
    """User-facing installer error."""


@dataclass(frozen=True)
class InstallPaths:
    dsh_home: Path
    home_patch: Path
    managed_dir: Path
    system_file: Path
    plugin_file: Path
    config_file: Path
    plugin_id_file: Path


@dataclass(frozen=True)
class InstallPlan:
    paths: InstallPaths
    source_system_file: Path
    activate: bool


def is_windows() -> bool:
    return os.name == "nt"


def expand_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    # Windows 上 pathlib 的 resolve() 可能返回正斜杠拼写,后续 read_text/
    # exists 对正斜杠 spell 的处理不一致;统一回原生反斜杠拼写。
    return Path(os.path.normpath(path)) if is_windows() else path


def resolve_dsh_home(explicit: str | Path | None = None) -> Path:
    """Resolve the Harness home: explicit > $DSH_HOME (non-blank) > ~/.dsh."""
    if explicit:
        return expand_path(explicit)
    env_home = os.environ.get(DEFAULT_DSH_HOME_ENV)
    if env_home and env_home.strip():
        return expand_path(env_home)
    return Path.home() / DEFAULT_DSH_HOME_DIR_NAME


def build_paths(dsh_home: Path | None = None, managed_subdir: str = DEFAULT_MANAGED_SUBDIR) -> InstallPaths:
    home = expand_path(dsh_home) if dsh_home else resolve_dsh_home()
    managed_dir = home / managed_subdir
    return InstallPaths(
        dsh_home=home,
        home_patch=home / DEFAULT_HOME_PATCH_NAME,
        managed_dir=managed_dir,
        system_file=managed_dir / DEFAULT_SYSTEM_FILE_NAME,
        plugin_file=managed_dir / DEFAULT_PLUGIN_NAME,
        config_file=managed_dir / DEFAULT_CONFIG_FILE_NAME,
        plugin_id_file=managed_dir / "plugin-id.txt",
    )


def normalize_system_prompt_content(content: str) -> str:
    """Strip GLM ChatML export markers, keeping the prompt body and its leading indent."""
    leading = content[: len(content) - len(content.lstrip())]
    text = content.lstrip()
    if text.startswith(IM_START_SYSTEM):
        text = text[len(IM_START_SYSTEM) :].lstrip("\r\n")
    elif text.startswith("<|im_start|>system"):
        text = text[len("<|im_start|>system") :].lstrip("\r\n")
    stripped = text.rstrip()
    if stripped.endswith(IM_END):
        text = stripped[: -len(IM_END)].rstrip() + "\n"
    return leading + text


def read_required_text(path: Path, label: str) -> str:
    if not path.exists():
        raise KeysmithError(f"{label} not found: {path}")
    if not path.is_file():
        raise KeysmithError(f"{label} is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise KeysmithError(f"{label} must be UTF-8: {path}") from exc
    except OSError as exc:
        raise KeysmithError(f"Could not read {label}: {path}\nReason: {exc}") from exc
    if not text.strip():
        raise KeysmithError(f"{label} is empty: {path}")
    return text


def read_system_prompt_source(path: Path) -> str:
    text = normalize_system_prompt_content(read_required_text(path, "source system prompt"))
    if not text.strip():
        raise KeysmithError(f"source system prompt is empty after normalization: {path}")
    return text


def yaml_escape(value: str) -> str:
    """Render a value as a YAML double-quoted scalar for a patch row field."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def render_patch_entry(plan: InstallPlan) -> str:
    """Render the single patch entry that mounts the managed plugin.

    A home-layer patch row either targets an existing entry (`- id: X` with
    config overrides) or *inserts* new rows. Our plugin is a brand-new row,
    so it must be an insert: `- insert:` carrying the full entry
    (id/name/config). A bare `- id: keysmith` row makes the loader treat it
    as an id-targeted override of a row that does not exist yet — the exact
    `patch: entry "keysmith" not found` warning a real `dsh --dump-config`
    surfaced.

    `id` stays stable across installs (patch reuse, duplicate-free merges).
    `name` is the loader module specifier. Home-layer patches append rows to
    the root include, whose relative base is the profile directory — so a
    relative `./keysmith/plugin.cjs` would resolve beside the profile, not
    the home (a real boot proved this). We therefore use an absolute
    `file://` URL for the plugin path, which Node's ESM loader resolves
    independently of any base.

    The `!!js` config expression injects the managed system-role.md path at
    entry activation. dsh's Loader evaluates `!!js` scalars via
    `with (ctx) { eval(expr) }` where the boot context provides
    `dshHomePath(...)` (from @deepseek-ai/dsh-home-paths) — the same hook
    dsh's own tests use (`!!js dshHomePath('sessions')`). `dshHomePath`
    resolves against the effective Harness home ($DSH_HOME or ~/.dsh), so
    the managed file is found even for a custom home. The expression is a
    flow mapping `{...}` whose embedded comma/colon require double-quoting
    the whole expression — the `!!js "..."` form dsh's own patches use.
    """
    expr = "{systemFile: dshHomePath('keysmith/system-role.md')}"
    plugin_url = "file:///" + plan.paths.plugin_file.as_posix()
    return (
        "- insert:\n"
        f"    - id: {PATCH_ROW_ID}\n"
        f"      name: {yaml_escape(plugin_url)}\n"
        f"      config:\n"
        f"        systemFile: !!js {yaml_escape(expr)}\n"
    )


def plugin_bundle_source() -> Path:
    """Path of the built plugin bundle.

    The plugin lives in this repo under plugin/ (`@deepseek-ai/dsh-keysmith`,
    TypeScript + Schemastery) and is built by `npm run build:bundle` (esbuild)
    into a single self-contained CommonJS file (plugin/dist/plugin.cjs). The
    installer deploys that artifact — not a hand-rendered template — so the
    deployed plugin is the real, typed, Schemastery-schema'd package. The
    built artifact is committed so installs need no node toolchain.
    """
    return REPO_ROOT / "plugin" / "dist" / "plugin.cjs"


def read_plugin_bundle() -> str:
    """Read the built plugin bundle, with a clear build hint when missing."""
    bundle = plugin_bundle_source()
    if not bundle.exists():
        raise KeysmithError(
            f"plugin bundle not found: {bundle}\n"
            "Build it first: cd plugin && npm install && npm run build:bundle"
        )
    return bundle.read_text(encoding="utf-8")


def render_config(plan: InstallPlan, system_file_sha256: str, plugin_file_sha256: str) -> str:
    payload = {
        "mode": "dsh-home-patch",
        "home_patch": str(plan.paths.home_patch),
        "managed_dir": str(plan.paths.managed_dir),
        "system_file": str(plan.paths.system_file),
        "system_file_sha256": system_file_sha256,
        "plugin_file": str(plan.paths.plugin_file),
        "plugin_file_sha256": plugin_file_sha256,
        "patch_row_id": PATCH_ROW_ID,
        "patch_plugin_name": PATCH_PLUGIN_NAME,
        "harness_bundle_modified": False,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.bak_{stamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak_{stamp}_{counter}")
        counter += 1
    path.replace(backup)
    return backup


def atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        handle.write(content)
        tmp = Path(handle.name)
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
    if mode is not None:
        path.chmod(mode)


def file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def write_lines(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if text:
        text += "\n"
    atomic_write_text(path, text)


def find_patch_entry(lines: list[str], row_id: str) -> int:
    """Index of the first patch row whose `id:` equals row_id, or -1."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"- id: {row_id}":
            return i
    return -1


def render_full_patch(plan: InstallPlan) -> str:
    entry = render_patch_entry(plan)
    lines = [
        "# Managed by deepseek-keysmith — do not edit this block by hand.",
        "# Installs the managed system-role entrypoint into every dsh profile.",
        entry.rstrip(),
    ]
    return "\n".join(lines) + "\n"


def merge_patch_entry(plan: InstallPlan) -> Path:
    """Idempotently merge the keysmith row into the home patch layer.

    Read -> backup -> write ordering matters: the existing content must be
    read before the file is renamed to a timestamped backup, then the merged
    result is written to the canonical path. Existing user rows are
    preserved; the keysmith row is replaced in place (so reinstalls never
    duplicate), appended at the end otherwise.
    """
    entry = render_patch_entry(plan).rstrip()
    target = plan.paths.home_patch
    if not target.exists():
        write_lines(target, render_full_patch(plan).splitlines())
        return target
    lines = read_lines(target)
    if not lines:
        write_lines(target, render_full_patch(plan).splitlines())
        return target
    backup_existing(target)
    index = find_patch_entry(lines, PATCH_ROW_ID)
    if index >= 0:
        # Replace the row plus its continuation lines (indented deeper than
        # the row itself). The row never closes a YAML list, so a following
        # non-indented line ends the row.
        end = index + 1
        while end < len(lines) and (lines[end].startswith("  ") or lines[end].startswith("\t")):
            end += 1
        lines[index:end] = entry.splitlines()
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append("")
        lines.extend(entry.splitlines())
    write_lines(target, lines)
    return target


def install_lines(plan: InstallPlan, dry_run: bool, backups: list[Path], activation: list[str]) -> list[str]:
    lines = ["deepseek-keysmith install preview" if dry_run else "deepseek-keysmith install complete"]
    lines.extend(
        [
            f"source_system_file: {plan.source_system_file}",
            f"dsh_home: {plan.paths.dsh_home}",
            f"home_patch: {plan.paths.home_patch}",
            f"managed_dir: {plan.paths.managed_dir}",
            f"system_file: {plan.paths.system_file}",
            f"plugin_file: {plan.paths.plugin_file}",
            f"config_file: {plan.paths.config_file}",
            f"patch_row_id: {PATCH_ROW_ID}",
            f"patch_plugin_name: {PATCH_PLUGIN_NAME}",
            f"system_role_order: {SYSTEM_ROLE_ORDER}",
            "harness_bundle_modified: false",
            "api_key: not read or stored",
            f"write: {str(not dry_run).lower()}",
        ]
    )
    if dry_run:
        lines.append("tip: rerun with install --yes to write these files")
    for backup in backups:
        lines.append(f"backup: {backup}")
    lines.extend(activation)
    if not dry_run:
        lines.append("effect: next dsh boot mounts the managed system-role section")
    return lines


def install(plan: InstallPlan, yes: bool, dry_run_flag: bool) -> list[str]:
    system_prompt = read_system_prompt_source(plan.source_system_file)
    dry_run = dry_run_flag or not yes
    if dry_run:
        return install_lines(plan, dry_run=True, backups=[], activation=[])

    plan.paths.managed_dir.mkdir(parents=True, exist_ok=True)
    # home_patch 由 merge_patch_entry 自行按"读->备份->写"处理。
    write_targets = [plan.paths.system_file, plan.paths.plugin_file, plan.paths.config_file]
    backups = [backup for target in write_targets if (backup := backup_existing(target))]

    atomic_write_text(plan.paths.system_file, system_prompt)
    atomic_write_text(plan.paths.plugin_file, read_plugin_bundle(), mode=0o644)
    merge_patch_entry(plan)
    atomic_write_text(
        plan.paths.config_file,
        render_config(
            plan,
            file_sha256(plan.paths.system_file) or "",
            file_sha256(plan.paths.plugin_file) or "",
        ),
    )
    return install_lines(plan, dry_run=False, backups=backups, activation=[])


def doctor_lines(paths: InstallPaths, source_system_file: Path) -> list[str]:
    system_hash = file_sha256(paths.system_file)
    plugin_hash = file_sha256(paths.plugin_file)
    patch_lines = read_lines(paths.home_patch)
    row_present = find_patch_entry(patch_lines, PATCH_ROW_ID) >= 0
    lines = [
        "deepseek-keysmith doctor",
        f"dsh_home: {paths.dsh_home}",
        f"home_patch: {paths.home_patch}",
        f"home_patch_exists: {str(paths.home_patch.exists()).lower()}",
        f"patch_row_present: {str(row_present).lower()}",
        f"system_file: {paths.system_file}",
        f"system_file_exists: {str(paths.system_file.exists()).lower()}",
        f"system_file_sha256: {system_hash or 'missing'}",
        f"plugin_file: {paths.plugin_file}",
        f"plugin_file_exists: {str(paths.plugin_file.exists()).lower()}",
        f"plugin_file_sha256: {plugin_hash or 'missing'}",
        f"config_file: {paths.config_file}",
        f"config_file_exists: {str(paths.config_file.exists()).lower()}",
        "harness_bundle_modified: false",
        "api_key: not read or stored",
    ]
    if row_present:
        detail = render_patch_entry(plan_for_lines(paths, source_system_file)).strip().splitlines()
        for detail_line in detail:
            lines.append(f"patch_row: {detail_line}")
    return lines


def plan_for_lines(paths: InstallPaths, source_system_file: Path) -> InstallPlan:
    return InstallPlan(paths=paths, source_system_file=source_system_file, activate=True)


def verify_lines(paths: InstallPaths, source_system_file: Path, check_module: bool = True) -> list[str]:
    system_hash = file_sha256(paths.system_file)
    plugin_hash = file_sha256(paths.plugin_file)
    patch_lines = read_lines(paths.home_patch)
    row_present = find_patch_entry(patch_lines, PATCH_ROW_ID) >= 0
    plugin_syntax_ok, plugin_detail = check_plugin_syntax(paths.plugin_file) if check_module else (False, "skipped")
    lines = [
        "deepseek-keysmith verify",
        f"dsh_home: {paths.dsh_home}",
        f"home_patch_exists: {str(paths.home_patch.exists()).lower()}",
        f"patch_row_present: {str(row_present).lower()}",
        f"system_file_exists: {str(paths.system_file.exists()).lower()}",
        f"system_file_sha256: {system_hash or 'missing'}",
        f"plugin_file_exists: {str(paths.plugin_file.exists()).lower()}",
        f"plugin_file_sha256: {plugin_hash or 'missing'}",
        f"plugin_syntax: {str(plugin_syntax_ok).lower()}",
        f"plugin_syntax_detail: {plugin_detail}",
        f"source_system_file: {source_system_file}",
        f"source_matches_managed: {str(file_sha256(source_system_file) == system_hash).lower()}",
        "harness_bundle_modified: false",
        "api_key: not read or stored",
    ]
    if row_present:
        lines.append("effect: next dsh boot mounts the managed system-role section")
    else:
        lines.append("effect: not mounted — run install to add the patch row")
    return lines


def check_plugin_syntax(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "plugin missing"
    node = shutil.which("node")
    if not node:
        return False, "node not found; syntax check skipped"
    completed = __import__("subprocess").run(
        [node, "--check", str(path)],
        text=True,
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
        check=False,
    )
    if completed.returncode == 0:
        return True, "ok"
    detail = (completed.stderr or completed.stdout).strip().splitlines()
    return False, detail[0] if detail else f"exit {completed.returncode}"


def uninstall_lines(paths: InstallPaths, dry_run: bool, removed: list[Path], activation: list[str]) -> list[str]:
    lines = ["deepseek-keysmith uninstall preview" if dry_run else "deepseek-keysmith uninstall complete"]
    for path in [paths.system_file, paths.plugin_file, paths.config_file, paths.home_patch]:
        lines.append(f"target: {path}")
    lines.append(f"write: {str(not dry_run).lower()}")
    for path in removed:
        lines.append(f"removed: {path}")
    lines.extend(activation)
    return lines


def uninstall(paths: InstallPaths, yes: bool, dry_run_flag: bool) -> list[str]:
    dry_run = dry_run_flag or not yes
    if dry_run:
        return uninstall_lines(paths, dry_run=True, removed=[], activation=[])
    removed = []
    for path in [paths.system_file, paths.plugin_file, paths.config_file]:
        if path.exists():
            backup = backup_existing(path)
            if backup:
                removed.append(backup)
    # Remove the keysmith row from the home patch layer, preserving other rows.
    if paths.home_patch.exists():
        lines = read_lines(paths.home_patch)
        index = find_patch_entry(lines, PATCH_ROW_ID)
        if index >= 0:
            end = index + 1
            while end < len(lines) and (lines[end].startswith("  ") or lines[end].startswith("\t")):
                end += 1
            del lines[index:end]
            if lines and lines[0].startswith("# Managed by deepseek-keysmith"):
                del lines[0:3]
            while lines and not lines[0].strip():
                del lines[0]
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                write_lines(paths.home_patch, lines)
            else:
                paths.home_patch.unlink()
            removed.append(paths.home_patch)
        else:
            # The home patch contains no keysmith row: if it was created by
            # this installer (header comment present) it is now empty of our
            # content and can be removed; otherwise leave the user's file.
            head = [line for line in lines if line.strip()]
            if head and head[0].startswith("# Managed by deepseek-keysmith"):
                backup = backup_existing(paths.home_patch)
                if backup:
                    removed.append(backup)
    return uninstall_lines(paths, dry_run=False, removed=removed, activation=[])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install or inspect deepseek-keysmith managed dsh system-role entrypoint.")
    sub = parser.add_subparsers(dest="command")

    install_parser = sub.add_parser("install", help="Install managed system-role patch row and files")
    install_parser.add_argument("--system-file", default=str(DEFAULT_SOURCE_SYSTEM_FILE), help="Source Markdown system prompt. Default: keysmith/system-role.md")
    install_parser.add_argument("--dsh-home", default=None, help="Harness home (default: $DSH_HOME or ~/.dsh)")
    install_parser.add_argument("--managed-subdir", default=DEFAULT_MANAGED_SUBDIR, help="Managed directory under the Harness home")
    install_parser.add_argument("--dry-run", action="store_true", help="Preview paths and checks without writing")
    install_parser.add_argument("--yes", action="store_true", help="Allow writing files. --dry-run wins if both are provided")

    doctor_parser = sub.add_parser("doctor", help="Inspect managed install state")
    doctor_parser.add_argument("--system-file", default=str(DEFAULT_SOURCE_SYSTEM_FILE))
    doctor_parser.add_argument("--dsh-home", default=None)
    doctor_parser.add_argument("--managed-subdir", default=DEFAULT_MANAGED_SUBDIR)

    verify_parser = sub.add_parser("verify", help="Verify the managed chain locally without model requests")
    verify_parser.add_argument("--system-file", default=str(DEFAULT_SOURCE_SYSTEM_FILE))
    verify_parser.add_argument("--dsh-home", default=None)
    verify_parser.add_argument("--managed-subdir", default=DEFAULT_MANAGED_SUBDIR)
    verify_parser.add_argument("--no-syntax", action="store_true", help="Skip node --check on the managed plugin")

    uninstall_parser = sub.add_parser("uninstall", help="Back up managed files and remove the patch row")
    uninstall_parser.add_argument("--dsh-home", default=None)
    uninstall_parser.add_argument("--managed-subdir", default=DEFAULT_MANAGED_SUBDIR)
    uninstall_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser.add_argument("--yes", action="store_true")

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        command = args.command or "doctor"
        paths = build_paths(args.dsh_home, args.managed_subdir)
        if command == "install":
            plan = InstallPlan(paths=paths, source_system_file=expand_path(args.system_file), activate=True)
            print("\n".join(install(plan, yes=args.yes, dry_run_flag=args.dry_run)))
            return 0
        if command == "doctor":
            print("\n".join(doctor_lines(paths, expand_path(args.system_file))))
            return 0
        if command == "verify":
            print("\n".join(verify_lines(paths, expand_path(args.system_file), check_module=not args.no_syntax)))
            return 0
        if command == "uninstall":
            print("\n".join(uninstall(paths, yes=args.yes, dry_run_flag=args.dry_run)))
            return 0
        parser.print_help()
        return 1
    except KeysmithError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
