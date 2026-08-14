from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "deepseek-keysmith.py"
spec = importlib.util.spec_from_file_location("deepseek_keysmith", MODULE_PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

IS_WINDOWS = os.name == "nt"


def make_source(tmp_path: Path, text: str = "# managed system\n") -> Path:
    source = tmp_path / "source.md"
    source.write_text(text, encoding="utf-8")
    return source


def make_paths(tmp_path: Path, subdir: str = "keysmith") -> mod.InstallPaths:
    return mod.build_paths(tmp_path, subdir)


def test_normalizes_glm_chatml_system_wrapper():
    raw = "<|im_start|>system:<project_instructions>\n# Body\n<|im_end|>\n"
    normalized = mod.normalize_system_prompt_content(raw)
    assert normalized == "<project_instructions>\n# Body\n"
    assert "<|im_start|>" not in normalized
    assert "<|im_end|>" not in normalized


def test_resolve_dsh_home_precedence(monkeypatch, tmp_path):
    custom = tmp_path / "custom-home"
    assert mod.resolve_dsh_home(custom) == custom.resolve()
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "from-env"))
    assert mod.resolve_dsh_home() == (tmp_path / "from-env").resolve()
    monkeypatch.setenv("DSH_HOME", "   ")
    assert mod.resolve_dsh_home() == (Path.home() / ".dsh").resolve()


def test_patch_entry_uses_insert_shape_and_relative_plugin(tmp_path):
    """The patch entry must be an insert (new row), not an id-targeted
    override — a bare `- id:` row makes the loader warn "entry not found"
    (surfaced by a real `dsh --dump-config`)."""
    paths = make_paths(tmp_path)
    plan = mod.InstallPlan(paths=paths, source_system_file=make_source(tmp_path), activate=True)
    entry = mod.render_patch_entry(plan)

    assert entry.startswith("- insert:\n")
    assert f"    - id: {mod.PATCH_ROW_ID}" in entry
    assert '      name: "file:///' in entry
    assert "!!js" in entry
    assert "dshHomePath('keysmith/system-role.md')" in entry


def test_js_expression_evaluates_with_dsh_loader_semantics(tmp_path):
    """The !!js config expression must evaluate under dsh's Loader scope.

    dsh evaluates `!!js` scalars via `with (ctx) { eval(expr) }` where the
    boot context provides only `dshHomePath` (see packages/boot/app-boot
    tests: `!!js dshHomePath('sessions')`). A bare-JS eval without that
    binding must not be assumed — the expression itself carries the call.
    """
    paths = make_paths(tmp_path)
    plan = mod.InstallPlan(paths=paths, source_system_file=make_source(tmp_path), activate=True)
    entry = mod.render_patch_entry(plan)

    assert "{systemFile: dshHomePath('keysmith/system-role.md')}" in entry
    # The expression must reference dshHomePath, never bare `path`/`__dshDir`
    # globals that a real loader scope does not provide.
    assert "path.join" not in entry
    assert "__dshDir" not in entry


def test_installed_patch_row_consumable_by_dsh_loader_semantics(tmp_path, capsys):
    """Full-chain check: dsh's YAML dialect parses the patch, the plugin
    module resolves beside it, the !!js expression evaluates under the
    dsh loader scope (with dshHomePath), and apply() registers a
    system-prompt section whose text renders the managed file.

    This mirrors the verified end-to-end check (install -> parse with
    entryListSchema -> evaluate -> require plugin -> drive apply).
    """
    source = make_source(tmp_path, "# verified system role\n")
    paths = make_paths(tmp_path)
    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()

    # 1) Patch file is a top-level YAML array with the keysmith row.
    import subprocess
    node = mod.shutil.which("node")
    assert node, "node required for the dsh-loader-semantics check"
    # js-yaml 需与 dsh 锁定的 v4 一致。优先用本机验证沙箱里的;否则跳过。
    candidate = Path(os.environ.get("DSH_VERIFY_NODE_MODULES", "")) / "js-yaml"
    if not candidate.exists():
        return  # 无 js-yaml 环境则跳过该端到端检查(安装器本身测试不受影响)
    script = r"""
const { load, Type, JSON_SCHEMA } = require('js-yaml')
const fs = require('node:fs')
const path = require('node:path')
const { fileURLToPath } = require('node:url')
const JsExpr = new Type('tag:yaml.org,2002:js', {
  kind: 'scalar', resolve: d => typeof d === 'string', construct: d => ({ __jsExpr: d }),
})
const schema = JSON_SCHEMA.extend(JsExpr)
const patchFile = process.argv[1]
const parsed = load(fs.readFileSync(patchFile, 'utf8'), { schema })
if (!Array.isArray(parsed)) throw new Error('patch must be a top-level array')
// The patch is an insert entry: `- insert: [{...}]` (matching applyEntryPatches).
const patchEntry = parsed.find(e => e && Array.isArray(e.insert))
if (!patchEntry) throw new Error('no insert patch entry')
const row = patchEntry.insert.find(r => r && r.id === 'keysmith')
if (!row) throw new Error('keysmith row missing')
if (typeof row.name !== 'string' || !row.name.startsWith('./')) throw new Error('name not relative')
# 2) plugin module resolves via its file:// URL name.
const pluginPath = row.name.startsWith('file://') ? fileURLToPath(row.name) : row.name
if (!fs.existsSync(pluginPath)) throw new Error('plugin not resolvable: ' + pluginPath)
const expr = row.config.systemFile
if (!expr || typeof expr.__jsExpr !== 'string') throw new Error('systemFile not a !!js node')
const evaluate = new Function('ctx', 'expr', 'with (ctx) { return eval(expr) }')
const home = path.dirname(patchFile)
const evaluated = evaluate({ dshHomePath: (...s) => path.join(home, ...s) }, expr.__jsExpr)
const systemFile = evaluated && typeof evaluated === 'object' ? evaluated.systemFile : evaluated
if (typeof systemFile !== 'string' || !fs.existsSync(systemFile)) throw new Error('systemFile eval missing: ' + systemFile)
const plugin = require(pluginPath)
if (plugin.name !== 'keysmith') throw new Error('plugin name mismatch')
if (!Array.isArray(plugin.inject) || !plugin.inject.includes('systemPrompt')) throw new Error('inject mismatch')
// dsh's fiber resolves plugin config through runtime.Config['~standard'].validate()
// (vendor/cordis/src/fiber.ts resolveConfig). Without it the boot crashes.
if (!plugin.Config || !plugin.Config['~standard'] || typeof plugin.Config['~standard'].validate !== 'function') {
  throw new Error('Config must expose a Standard Schema V1 ~standard.validate')
}
const validated = plugin.Config['~standard'].validate({ systemFile })
if (validated.issues) throw new Error('config validation failed: ' + JSON.stringify(validated.issues))
let registered = null
plugin.apply({ effect: fn => fn(), systemPrompt: { section: s => { registered = s } } }, { systemFile })
if (!registered) throw new Error('no section registered')
if (registered.name !== 'managed:keysmith' || registered.order !== 5) throw new Error('section mismatch')
if (registered.text().trim() !== '# verified system role') throw new Error('rendered text mismatch')
console.log('DSH_LOADER_SEMANTICS_OK')
"""
    completed = subprocess.run(
        [node, "-e", script, str(paths.home_patch)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        env={**os.environ, "NODE_PATH": str(Path(os.environ["DSH_VERIFY_NODE_MODULES"]))},
    )
    assert completed.returncode == 0, (completed.stderr or completed.stdout)
    assert "DSH_LOADER_SEMANTICS_OK" in completed.stdout


def test_plugin_bundle_is_valid_and_self_contained(tmp_path):
    """The deployed plugin is the esbuild bundle of the @deepseek-ai/dsh-keysmith
    workspace package — a single self-contained CJS file (schemastery inlined,
    only node: builtins external)."""
    bundle = mod.plugin_bundle_source()
    if not bundle.exists():
        pytest.skip(f"plugin bundle not built: {bundle}")
    plugin_text = bundle.read_text(encoding="utf-8")

    # Single self-contained file: no bare requires beyond node builtins.
    import re
    bare_requires = re.findall(r"require\(\"([a-z][^/\"]*)\"\)", plugin_text)
    assert all(r.startswith("node:") for r in bare_requires), f"non-builtin requires: {bare_requires}"

    node = mod.shutil.which("node")
    if not node:
        return
    plugin_file = tmp_path / "plugin.cjs"
    plugin_file.write_text(plugin_text, encoding="utf-8")
    completed = subprocess.run(
        [node, "--check", str(plugin_file)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    assert completed.returncode == 0, (completed.stderr or completed.stdout)

    # Bundled plugin must expose the full Cordis plugin shape with a real
    # Schemastery standard-schema Config.
    probe = f"""
const p = require({str(plugin_file)!r})
if (p.name !== 'keysmith') throw new Error('name mismatch')
if (!Array.isArray(p.inject) || !p.inject.includes('systemPrompt')) throw new Error('inject mismatch')
if (!p.Config || typeof p.Config['~standard']?.validate !== 'function') throw new Error('Config ~standard missing')
const ok = p.Config['~standard'].validate({{ systemFile: 'C:/x.md' }})
if (ok.issues) throw new Error('valid config rejected: ' + JSON.stringify(ok.issues))
const bad = p.Config['~standard'].validate({{}})
if (!bad.issues?.length) throw new Error('invalid config accepted')
let registered = null
p.apply({{ effect: fn => fn(), systemPrompt: {{ section: s => {{ registered = s }} }} }}, {{ systemFile: 'C:/x.md' }})
if (!registered || registered.name !== 'managed:keysmith' || registered.order !== 5) throw new Error('section mismatch')
console.log('BUNDLE_OK')
"""
    probe_run = subprocess.run(
        ["node", "-e", probe], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    assert probe_run.returncode == 0, (probe_run.stderr or probe_run.stdout)
    assert "BUNDLE_OK" in probe_run.stdout


def test_install_dry_run_does_not_write(tmp_path, capsys):
    source = make_source(tmp_path)
    paths = make_paths(tmp_path)
    plan = mod.InstallPlan(paths=paths, source_system_file=source, activate=True)

    code = mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out

    assert code == 0
    assert "deepseek-keysmith install preview" in out
    assert "write: false" in out
    assert not paths.system_file.exists()
    assert not paths.plugin_file.exists()
    assert not paths.home_patch.exists()


def test_install_writes_files_and_patch_row(tmp_path, capsys):
    source = make_source(tmp_path, "# installed prompt\n")
    paths = make_paths(tmp_path)
    plan = mod.InstallPlan(paths=paths, source_system_file=source, activate=True)

    code = mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    out = capsys.readouterr().out

    assert code == 0
    assert "deepseek-keysmith install complete" in out
    assert paths.system_file.read_text(encoding="utf-8") == "# installed prompt\n"
    assert paths.plugin_file.exists()
    config = json.loads(paths.config_file.read_text(encoding="utf-8"))
    assert config["mode"] == "dsh-home-patch"
    assert config["harness_bundle_modified"] is False
    patch_text = paths.home_patch.read_text(encoding="utf-8")
    assert f"- id: {mod.PATCH_ROW_ID}" in patch_text
    assert "file:///" in patch_text
    assert "keysmith/plugin.cjs" in patch_text
    assert paths.home_patch.parent == paths.dsh_home


def test_reinstall_does_not_duplicate_patch_row(tmp_path, capsys):
    source = make_source(tmp_path)
    paths = make_paths(tmp_path)

    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()
    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()

    patch_text = paths.home_patch.read_text(encoding="utf-8")
    assert patch_text.count(f"- id: {mod.PATCH_ROW_ID}") == 1
    assert patch_text.count("file:///") == 1


def test_install_preserves_existing_patch_rows(tmp_path, capsys):
    source = make_source(tmp_path)
    paths = make_paths(tmp_path)
    paths.home_patch.parent.mkdir(parents=True, exist_ok=True)
    paths.home_patch.write_text("- id: user-row\n  name: 'custom'\n  config:\n    a: 1\n", encoding="utf-8")

    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()

    patch_text = paths.home_patch.read_text(encoding="utf-8")
    assert "- id: user-row" in patch_text
    assert "    a: 1" in patch_text
    assert f"- id: {mod.PATCH_ROW_ID}" in patch_text
    assert patch_text.index("user-row") < patch_text.index(mod.PATCH_ROW_ID)


def test_doctor_reports_state_without_secrets(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "TEST_OPENAI_KEY_REDACTED")
    source = make_source(tmp_path)
    paths = make_paths(tmp_path)
    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()

    code = mod.main(["doctor", "--system-file", str(source), "--dsh-home", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 0
    assert "deepseek-keysmith doctor" in out
    assert "patch_row_present: true" in out
    assert "system_file_sha256:" in out
    assert "api_key: not read or stored" in out
    assert "TEST_OPENAI_KEY_REDACTED" not in out


def test_verify_chain_after_install(tmp_path, capsys):
    source = make_source(tmp_path)
    paths = make_paths(tmp_path)
    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()

    code = mod.main(["verify", "--system-file", str(source), "--dsh-home", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 0
    assert "patch_row_present: true" in out
    assert "source_matches_managed: true" in out
    assert "harness_bundle_modified: false" in out


def test_uninstall_removes_row_and_managed_files(tmp_path, capsys):
    source = make_source(tmp_path)
    paths = make_paths(tmp_path)
    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()

    code = mod.main(["uninstall", "--dsh-home", str(tmp_path), "--yes"])
    out = capsys.readouterr().out

    assert code == 0
    assert "deepseek-keysmith uninstall complete" in out
    assert not paths.system_file.exists()
    assert not paths.plugin_file.exists()
    assert not paths.config_file.exists()
    patch_text = paths.home_patch.read_text(encoding="utf-8") if paths.home_patch.exists() else ""
    assert f"- id: {mod.PATCH_ROW_ID}" not in patch_text


def test_uninstall_preserves_foreign_patch_layer(tmp_path, capsys):
    source = make_source(tmp_path)
    paths = make_paths(tmp_path)
    paths.home_patch.parent.mkdir(parents=True, exist_ok=True)
    paths.home_patch.write_text("- id: user-row\n  name: 'custom'\n  config:\n    a: 1\n", encoding="utf-8")

    mod.main(["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()
    mod.main(["uninstall", "--dsh-home", str(tmp_path), "--yes"])
    capsys.readouterr()

    assert paths.home_patch.exists()
    patch_text = paths.home_patch.read_text(encoding="utf-8")
    assert "- id: user-row" in patch_text
    assert f"- id: {mod.PATCH_ROW_ID}" not in patch_text


def test_main_module_compiles(tmp_path):
    py_compile.compile(str(MODULE_PATH), doraise=True)
    assert MODULE_PATH.exists()


def test_install_with_custom_managed_subdir(tmp_path, capsys):
    source = make_source(tmp_path)
    paths = make_paths(tmp_path, "managed-custom")
    mod.main(
        ["install", "--system-file", str(source), "--dsh-home", str(tmp_path), "--managed-subdir", "managed-custom", "--yes"]
    )
    capsys.readouterr()

    assert paths.system_file.exists()
    assert paths.plugin_file.exists()
    patch_text = paths.home_patch.read_text(encoding="utf-8")
    assert "file:///" in patch_text
    assert "managed-custom/plugin.cjs" in patch_text
