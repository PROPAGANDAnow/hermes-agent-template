"""Prepare the template image using the pinned release's own PM/build APIs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


PAYLOAD = Path("/opt/hermes")
SOURCE = PAYLOAD / "repo"
ENVIRONMENT = PAYLOAD / "venv"
WRAPPER = PAYLOAD / "wrapper-venv"
EXTRAS = ("all", "messaging", "tts-premium", "bedrock", "anthropic", "edge-tts", "vision")


def run(*args: str | Path, **kwargs) -> None:
    subprocess.run([str(arg) for arg in args], cwd=SOURCE, check=True, **kwargs)


def tools() -> None:
    from pm import ensure, env_for, installed_package, stage_manager_runtime
    from scripts.bundles.payload import seal_pm_runtime
    from scripts.releases.distance import NoReleaseTag, dev_version
    from scripts.write_install_stamp import write_stamp

    for name in ("python", "uv", "npm", "ffmpeg", "ripgrep"):
        ensure(name, explicit=True)
    for command, package in (("python3", "python"), ("node", "node"), ("npm", "npm"),
                             ("ffmpeg", "ffmpeg"), ("rg", "ripgrep")):
        Path("/usr/local/bin", command).symlink_to(installed_package(package).binary)
    npx = shutil.which("npx", path=env_for("npm", base_env={})["PATH"])
    if npx is None:
        raise RuntimeError("Pinned npm does not provide npx")
    Path("/usr/local/bin/npx").symlink_to(npx)
    Path("/usr/local/bin/ffprobe").symlink_to(installed_package("ffmpeg").binary.with_name("ffprobe"))
    python = installed_package("python").binary.resolve()
    stage_manager_runtime(python=python, destination=PAYLOAD / "pm-runtime",
                          project=SOURCE / "pm", cache=PAYLOAD / "uv-cache")
    seal_pm_runtime(PAYLOAD, python)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    # pyproject intentionally declares 0.0.0. Match the official Docker workflow:
    # resolve the release identity from Git, then persist it before removing Git.
    try:
        display = dev_version(SOURCE)
    except NoReleaseTag:
        display = None
    base = display.partition("+")[0] if display else None
    distance = int(display.partition("+")[2].partition(".")[0]) if display and "+" in display else 0
    stamp = write_stamp(SOURCE / "install-stamp.json", commit=commit, dirty=False,
                        base_version=base, display_version=display, distance=distance, source="docker",
                        distribution="docker", update_mechanism="external",
                        runtime_dir=str(PAYLOAD / "tools"))
    stamp["pmRuntime"] = str(PAYLOAD / "pm-runtime")
    (SOURCE / "install-stamp.json").write_text(json.dumps(stamp, indent=2) + "\n")
    (SOURCE / ".install_method").write_text("docker\n")
    marker = Path("/etc/hermes/image-provenance.json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"schema": 1, "deployment_kind": "image", "manager": "docker",
                                  "image": "hermes-agent-template", "version": base,
                                  "revision": commit}, sort_keys=True) + "\n")
    marker.chmod(0o444)


def dependencies() -> None:
    run(sys.executable, "-m", "pm.build_env", "--source", SOURCE, "--python", sys.executable,
        "--cache", PAYLOAD / "uv-cache", "--out", ENVIRONMENT,
        "--no-install-project", "--sealed", "--record-selection",
        *(arg for extra in EXTRAS for arg in ("--extra", extra)))
    # The parent owns a fixed image environment. It never activates a writable PM
    # generation; its explicit .pth reuses only the immutable, frozen base graph.
    run(sys.executable, "-m", "venv", "--without-pip", WRAPPER)
    site = next(ENVIRONMENT.glob("lib/python*/site-packages"))
    wrapper_site = next(WRAPPER.glob("lib/python*/site-packages"))
    (wrapper_site / "hermes-template.pth").write_text(f"{site}\n{SOURCE}\n")
    run(WRAPPER / "bin/python", "-I", "-c", """
from importlib.metadata import version
from pathlib import Path
from packaging.requirements import Requirement
for line in Path('/app/requirements.txt').read_text().splitlines():
    if not line.strip() or line.lstrip().startswith('#'):
        continue
    requirement = Requirement(line)
    if requirement.marker is None or requirement.marker.evaluate():
        assert version(requirement.name) in requirement.specifier, requirement
import httpx, jinja2, multipart, starlette, uvicorn, websockets, hermes_yaml
from hermes_cli.config import atomic_config_replace
""")


def frontends() -> None:
    run(ENVIRONMENT / "bin/python", "-I", SOURCE / "scripts/generate_icons.py",
        "--source", SOURCE, "--out", "/tmp/hermes-icons")
    run("node", "scripts/build/node-deps.mjs", "--source", SOURCE,
        "--workspace", "ui-tui", "--workspace", "web")
    run("node", "scripts/build/tui.mjs", "--source", SOURCE, "--out", "/tmp/hermes-products/tui")
    run("node", "scripts/build/web.mjs", "--source", SOURCE,
        "--icons", "/tmp/hermes-icons", "--out", "/tmp/hermes-products/web")
    shutil.copytree("/tmp/hermes-products/tui", PAYLOAD / "ui-tui")
    shutil.copytree("/tmp/hermes-products/web", SOURCE / "hermes_cli/web_dist", dirs_exist_ok=True)
    # Keep TypeScript for the native linting feature; bundler dependencies are
    # build inputs, and the compiled TUI includes its runtime JS dependencies.
    typescript = Path("/tmp/hermes-typescript")
    shutil.move(SOURCE / "node_modules/typescript", typescript)
    for name in ("node_modules", "web", "ui-tui"):
        shutil.rmtree(SOURCE / name)
    shutil.move(typescript, SOURCE / "node_modules/typescript")
    (SOURCE / "node_modules/.bin").mkdir()
    (SOURCE / "node_modules/.bin/tsc").symlink_to("../typescript/bin/tsc")
    shutil.rmtree("/tmp/hermes-products")
    shutil.rmtree("/tmp/hermes-icons")


def assemble() -> None:
    from hermes_cli._launchers import stage_launcher
    from pm import installed_package
    from pm.store import current_target
    from scripts.build.agent import assemble as assemble_agent
    from scripts.build.inputs import AgentInputs, RESOURCE_ENV

    assemble_agent(AgentInputs(
        project=SOURCE / "pyproject.toml", code=SOURCE, repo="repo", placement="contained",
        target=current_target(), python=installed_package("python").binary.resolve(),
        site_packages=next(ENVIRONMENT.glob("lib/python*/site-packages")),
        environment=ENVIRONMENT, tools=PAYLOAD / "tools", pm_runtime=PAYLOAD / "pm-runtime",
        resources={name: SOURCE / name for name in RESOURCE_ENV},
        frontends={"tui": PAYLOAD / "ui-tui", "web": SOURCE / "hermes_cli/web_dist"},
        stamp=SOURCE / "install-stamp.json",
    ), PAYLOAD)
    # The generic assembler's inline bootstrap is not recognized by this
    # release's live gateway identity matcher. Use the native publisher for
    # both PATH commands and installation_command()'s source-local commands.
    for directory in (PAYLOAD / "bin", SOURCE / ".hermes/bin"):
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("hermes", "hermes-acp"):
            if stage_launcher(name, SOURCE, directory) is None:
                raise RuntimeError(f"Cannot publish native launcher: {directory / name}")
    # Retain the template's established code path for migration hooks/operators.
    Path("/opt/hermes-agent").symlink_to(SOURCE, target_is_directory=True)
    shutil.rmtree(SOURCE / ".git")


def verify() -> None:
    from pm import installed_package
    from pm.environments import payload_venv
    from pm.runtime import _resident_runtime

    assert payload_venv(SOURCE) == ENVIRONMENT
    python, site = _resident_runtime()
    run(python, "-I", "-S", "-c",
        f"import sys; sys.path[:0] = {[str(site), str(SOURCE)]!r}; "
        "import packaging, tomli_w, truststore; from ruamel.yaml import YAML")
    sqlite_check = """
import json, sqlite3, sys, tempfile
from pathlib import Path
assert sqlite3.sqlite_version_info >= (3, 51, 3), sqlite3.sqlite_version
if sys.argv[1]:
    assert sqlite3.sqlite_version == sys.argv[1], sqlite3.sqlite_version
with tempfile.TemporaryDirectory() as directory:
    with sqlite3.connect(Path(directory) / 'test.db') as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')")
        db.execute("INSERT INTO docs VALUES ('hermes')")
        assert db.execute("SELECT count(*) FROM docs WHERE docs MATCH 'erm'").fetchone()[0] == 1
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
"""
    # The PM Python embeds SQLite3.53.1. It already includes the WAL-reset fix
    # (SQLite3.51.3/3.53.0); replacing the distro shared library cannot affect it.
    # Exercise Hermes' actual FTS triggers/flush probe, rather than only imports.
    native_check = f"""
sys.path.insert(0, {str(SOURCE)!r})
from pm.environments import activate_dependencies
activate_dependencies(Path({str(SOURCE)!r}))
from hermes_state import SessionDB
from hermes_state_repair import _db_opens_cleanly
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'state.db'
    db = SessionDB(path)
    assert db._fts_enabled and db._trigram_available
    db.create_session('image-sqlite-check', 'cli')
    db.append_message('image-sqlite-check', 'user', 'Hermes SQLite image test')
    db.close()
    issue = _db_opens_cleanly(path)
    assert issue is None, issue
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM messages_fts_trigram WHERE messages_fts_trigram MATCH 'erm'").fetchone()[0] == 1
print(json.dumps({{'python': sys.executable, 'sqlite': sqlite3.sqlite_version,
                  'wal_fts5_trigram_integrity': 'ok', 'native_write_health_flush': 'ok'}}))
"""
    check_env = {**os.environ, "HERMES_HOME": "/tmp/hermes-image-check",
                 "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    expected_sqlite = "3.53.1" if os.environ.get("HERMES_REF") == "v0.22.2" else ""
    for executable in (installed_package("python").binary, ENVIRONMENT / "bin/python", WRAPPER / "bin/python"):
        run(executable, "-I", "-c", sqlite_check + native_check, expected_sqlite, env=check_env)
    # Keep the fixed shared library for distro Python and other dynamic clients.
    run("/usr/bin/python3", "-I", "-c", sqlite_check, "3.53.4")
    run(ENVIRONMENT / "bin/python", "-I", "-c", """
import shlex
from pathlib import Path
from gateway.status import _gateway_command_subcommand
for directory in (Path('/opt/hermes/bin'), Path('/opt/hermes/repo/.hermes/bin')):
    tokens = shlex.split((directory / 'hermes').read_text(), comments=True)
    assert tokens[0] == 'exec' and tokens[-1] == '$@'
    for verb in ('run', 'restart'):
        command = tokens[1:-1] + ['-p', 'default', 'gateway', verb]
        assert _gateway_command_subcommand(' '.join(command)) == verb, directory
""")
    run(PAYLOAD / "bin/hermes", "--version", env=check_env)
    # A fresh native detached action selects the bare store interpreter. Verify
    # that it can activate the recognized base before any writable generation.
    run(python, "-I", "-c",
        f"import sys; from pathlib import Path; sys.path.insert(0, {str(SOURCE)!r}); "
        f"from pm.environments import activate_dependencies; activate_dependencies(Path({str(SOURCE)!r})); "
        "import fastapi, hermes_yaml; from pm import ensure; ensure('node')",
        env=check_env)
    run(WRAPPER / "bin/python", "-I", "-c",
        "from importlib.metadata import version; assert version('hermes-agent') == "
        + repr(tomllib.loads((SOURCE / "pyproject.toml").read_text())["project"]["version"]))
    run(WRAPPER / "bin/python", "-I", "-c", """
import json, os
from pathlib import Path
from hermes_cli.version_info import get_version_info
stamp = json.loads(Path('/opt/hermes/repo/install-stamp.json').read_text())
info = get_version_info()
assert info.commit == stamp['commit']
assert info.base_version == (stamp['baseVersion'] or 'unknown')
if os.environ.get('HERMES_REF') == 'v0.22.2':
    assert info.base_version == '0.22.2', info
""")
    run("node", "--version")
    run("npm", "--version")
    run("ffmpeg", "-version", stdout=subprocess.DEVNULL)
    run("ffprobe", "-version", stdout=subprocess.DEVNULL)
    run("rg", "--version", stdout=subprocess.DEVNULL)
    shutil.rmtree("/tmp/hermes-image-check", ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("tools", "dependencies", "frontends", "assemble", "verify"))
    args = parser.parse_args()
    sys.path.insert(0, str(SOURCE))
    globals()[args.step]()
