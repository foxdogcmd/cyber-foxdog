"""A1/A3 修复：http_request verify 参数；环境感知系统提示。"""
from __future__ import annotations

import httpx

from foxdog.core import envprobe, prompts
from foxdog.executor.background import BackgroundRegistry
from foxdog.store.workspace import Workspace
from foxdog.swarm.facts import FactsBoard
from foxdog.tools import ToolContext
from foxdog.tools.builtin.http import HttpArgs, HttpRequestTool


def test_verify_defaults_false():
    assert HttpArgs(url="https://x").verify is False
    assert HttpArgs(url="https://x", verify=True).verify is True


async def test_verify_passed_to_httpx(tmp_path, monkeypatch):
    captured: dict = {}

    class FakeResp:
        status_code = 200
        reason_phrase = "OK"
        headers: dict = {}
        text = "hi"

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def request(self, *a, **kw):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    ctx = ToolContext(
        workspace=Workspace(base=tmp_path, run_id="http-test"),
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )
    res = await HttpRequestTool().run({"url": "https://self-signed.target/"}, ctx)
    assert res.ok and "hi" in res.output
    assert captured.get("verify") is False  # CTF 默认跳过证书验证

    await HttpRequestTool().run({"url": "https://x/", "verify": True}, ctx)
    assert captured.get("verify") is True


def test_env_probe_and_prompt_block():
    env = envprobe.probe_env()
    assert env.os and env.shell and env.python_cmd
    text = envprobe.render_env(env)
    assert "运行环境事实" in text
    assert env.shell in text and env.python_cmd in text
    assert "已安装安全工具" in text
    docker_env = envprobe.EnvFacts(
        os="Linux",
        is_windows=False,
        shell="sh -c",
        python_cmd="/usr/bin/python3",
        python3_on_path=True,
        tmp_dir="/tmp",
        runtime_kind="docker",
        runtime_workdir="/workspace/work",
    )
    docker_text = envprobe.render_env(docker_env)
    assert "AGENTS.md" in docker_text


def test_system_prompt_env_and_discipline():
    env = envprobe.EnvFacts(
        os="Linux",
        is_windows=False,
        shell="sh -c",
        python_cmd="/usr/bin/python3",
        python3_on_path=True,
        tmp_dir="/tmp",
        runtime_kind="docker",
        runtime_workdir="/workspace/work",
    )
    prompt = prompts.build_system_prompt(env=env, board=FactsBoard("/nonexistent/facts.jsonl"))
    # 环境事实块
    assert "运行环境事实" in prompt
    assert "AGENTS.md" in prompt
    # 环境纪律：先确认环境再写脚本
    assert "先确认环境再写脚本" in prompt
    # 事实板纪律与区块
    assert "开工先查死路" in prompt
    assert "已知事实" in prompt and "已排除方向" in prompt
    assert "post_fact" in prompt and "post_deadend" in prompt
    # 技能与外部工具纪律
    assert "activate_skill" in prompt and "select_tools" in prompt
    # 未完成时继续调用工具，减少纯总结造成的新 turn/API 往返
    assert "不要只发阶段总结" in prompt and "无意义的新 turn" in prompt


def test_windows_prompt_prefers_python_for_complex_work():
    env = envprobe.EnvFacts(
        os="Windows",
        is_windows=True,
        shell="pwsh -Command",
        python_cmd=r"C:\Python\python.exe",
        python3_on_path=False,
        tmp_dir=r"C:\Temp",
    )
    rendered = envprobe.render_env(env)
    prompt = prompts.build_system_prompt(env=env)
    assert "以下项目均为本机实测" in rendered
    assert "短逻辑用 python_eval" in rendered and ".py" in rendered
    assert env.python_cmd in rendered
    assert "PowerShell 主要用于短原生命令" in rendered
    assert "直接用 python_eval" in prompt and "需要复用、调试或包含多步逻辑时" in prompt
    assert "不要凭 OS 猜测" in prompt and "避免长脚本及嵌套引号" in prompt
