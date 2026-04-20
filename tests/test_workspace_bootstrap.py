import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path


def load_server(tmp_path: Path):
    hermes_home = tmp_path / ".hermes"
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["ADMIN_PASSWORD"] = "test-admin-password"

    module_name = f"server_test_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[1] / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_workspace_bootstrap_creates_expected_layout(tmp_path):
    server = load_server(tmp_path)

    state = server.ensure_workspace_layout()

    root = tmp_path / ".hermes" / "workspaces"
    assert root == Path(state["root"])
    assert state["ready"] is True
    assert state["default_cwd"] == str(root / "default")

    for name in ("default", "projects", "scratch", "shared"):
        assert (root / name).is_dir()
        assert state["workspaces"][name]["exists"] is True

    metadata_path = root / ".bootstrap.json"
    assert metadata_path.exists()
    metadata = json.loads(metadata_path.read_text())
    assert metadata["default_cwd"] == str(root / "default")


def test_write_config_yaml_uses_persistent_workspace_default_cwd(tmp_path):
    server = load_server(tmp_path)
    server.ensure_workspace_layout()

    server.write_config_yaml({"LLM_MODEL": "google/gemma-3-1b-it:free"})

    config_text = (tmp_path / ".hermes" / "config.yaml").read_text()
    assert 'cwd: "{}"'.format(tmp_path / ".hermes" / "workspaces" / "default") in config_text
    assert 'data_dir: "{}"'.format(tmp_path / ".hermes") in config_text


def test_config_complete_requires_workspace_model_provider_and_channel(tmp_path):
    server = load_server(tmp_path)

    server.ensure_workspace_layout()
    assert server.is_config_complete({}) is False
    assert server.is_config_complete({
        "LLM_MODEL": "model",
        "OPENROUTER_API_KEY": "key",
    }) is False
    assert server.is_config_complete({
        "LLM_MODEL": "model",
        "TELEGRAM_BOT_TOKEN": "token",
    }) is False
    assert server.is_config_complete({
        "LLM_MODEL": "model",
        "OPENROUTER_API_KEY": "key",
        "TELEGRAM_BOT_TOKEN": "token",
    }) is True


def test_api_status_includes_setup_checklist_and_workspace_state(tmp_path):
    server = load_server(tmp_path)
    server.ensure_workspace_layout()
    server.write_env(server.ENV_FILE, {
        "LLM_MODEL": "model",
        "OPENROUTER_API_KEY": "key",
        "TELEGRAM_BOT_TOKEN": "token",
    })

    server.guard = lambda request: None
    response = asyncio.run(server.api_status(None))
    payload = json.loads(response.body)

    assert payload["setup"]["ready"] is True
    assert payload["setup"]["checklist"] == {
        "workspace_layout": True,
        "model_configured": True,
        "provider_configured": True,
        "channel_configured": True,
    }
    assert payload["workspaces"]["default_cwd"] == str(tmp_path / ".hermes" / "workspaces" / "default")
    assert payload["workspaces"]["workspaces"]["projects"]["exists"] is True


def test_config_reset_preserves_workspace_directories(tmp_path):
    server = load_server(tmp_path)
    workspace_state = server.ensure_workspace_layout()
    server.write_env(server.ENV_FILE, {
        "LLM_MODEL": "model",
        "OPENROUTER_API_KEY": "key",
        "TELEGRAM_BOT_TOKEN": "token",
    })

    server.guard = lambda request: None
    asyncio.run(server.api_config_reset(None))

    assert not server.ENV_FILE.exists()
    for name in workspace_state["workspaces"]:
        assert (tmp_path / ".hermes" / "workspaces" / name).is_dir()
