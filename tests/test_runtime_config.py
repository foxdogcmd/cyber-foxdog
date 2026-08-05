from __future__ import annotations

import pytest
from pydantic import ValidationError

from foxdog.config import AppConfig, ConfigStore, load_config
from foxdog.tools import ToolRegistry
from foxdog.tools.builtin.config_edit import register_config_tools


def _isolate_config(tmp_path, monkeypatch, toml: str = "") -> None:
    user_config = tmp_path / "user.toml"
    if toml:
        user_config.write_text(toml, encoding="utf-8")
    monkeypatch.setattr("foxdog.config.USER_CONFIG", user_config)
    monkeypatch.setattr("foxdog.config.PROJECT_CONFIG", tmp_path / "missing-project.toml")
    monkeypatch.delenv("FOXDOG_RUNTIME", raising=False)


def test_runtime_defaults_keep_local_execution():
    runtime = AppConfig().runtime

    assert runtime.kind == "local"
    assert runtime.image == "foxdog-runtime:full"
    assert runtime.docker_binary == "docker"
    assert runtime.network == "bridge"
    assert runtime.mounts == []
    assert runtime.auto_build is True
    assert runtime.auto_remove is True
    assert runtime.workdir == "/workspace/work"

    runtime.mounts.append("one-run-only")
    assert AppConfig().runtime.mounts == []

    claude = AppConfig().engines.claude_code
    assert claude.enabled is False
    assert claude.containerized is True
    assert claude.import_host_config is False


def test_loads_runtime_table(tmp_path, monkeypatch):
    _isolate_config(
        tmp_path,
        monkeypatch,
        """
[runtime]
kind = "docker"
image = "registry.example/foxdog-runtime:pwn"
docker_binary = "podman"
network = "host"
mounts = ["./challenge:/workspace/inputs:ro", "./cache:/cache"]
auto_build = false
auto_remove = false
workdir = "/workspace/custom"
""",
    )

    runtime = load_config().runtime

    assert runtime.kind == "docker"
    assert runtime.image == "registry.example/foxdog-runtime:pwn"
    assert runtime.docker_binary == "podman"
    assert runtime.network == "host"
    assert runtime.mounts == ["./challenge:/workspace/inputs:ro", "./cache:/cache"]
    assert runtime.auto_build is False
    assert runtime.auto_remove is False
    assert runtime.workdir == "/workspace/custom"


def test_runtime_environment_then_cli_override(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch, '[runtime]\nkind = "local"\n')
    monkeypatch.setenv("FOXDOG_RUNTIME", "docker")

    assert load_config().runtime.kind == "docker"
    assert load_config(["runtime.kind=local"]).runtime.kind == "local"


def test_runtime_kind_rejects_unknown_backend(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch, '[runtime]\nkind = "remote"\n')

    with pytest.raises(ValidationError):
        load_config()


def test_runtime_config_store_and_setting_tool_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    store = ConfigStore(path)
    store.set_runtime(
        kind="docker",
        image="registry.example/foxdog:pwn",
        mounts=["C:/ctf:/data:ro"],
        auto_build=False,
    )
    store.save()

    loaded = ConfigStore(path)
    assert loaded.data["runtime"]["kind"] == "docker"
    assert loaded.data["runtime"]["mounts"] == ["C:/ctf:/data:ro"]

    registry = ToolRegistry()
    register_config_tools(registry, loaded)
    assert registry.get("set_runtime") is not None
