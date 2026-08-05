"""异构引擎竞速：多端点配置 round-trip、race 扇出 first-valid-flag-wins、CLI 引擎收割。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from foxdog.config import AppConfig, ConfigStore, ModelEndpoint
from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget
from foxdog.core.goal import GoalMode
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import system_message, user_message
from foxdog.store.workspace import Workspace
from foxdog.swarm.claude_config import ClaudeConfigError, load_claude_code_env
from foxdog.swarm.cli_engine import CliEngine
from foxdog.swarm.coordinator import Coordinator
from foxdog.swarm.engines import plan_race_engines
from foxdog.swarm.planner import ReasonPlanner
from foxdog.swarm.worker import WorkerFactory
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools
from foxdog.tools.builtin.config_edit import ConfigureClaudeCodeTool, RemoveModelEndpointTool, SetModelTool

FLAG = "flag{engine_race_ok}"


# ------------------------------------------------------------ 任务 1：多端点配置
def test_extra_endpoint_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    store = ConfigStore(path)
    store.set_model("extra:kimi", protocol="openai", base_url="https://api.moonshot.cn/v1", api_key="sk-kimi", model="kimi-k2")
    store.set_model("extra:ds", protocol="anthropic", base_url="https://api.deepseek.com/anthropic", api_key="sk-ds", model="deepseek-v4-flash")
    store.save()

    back = ConfigStore(path)
    kimi = back.data["model"]["extra"]["kimi"]
    assert kimi["model"] == "kimi-k2" and kimi["protocol"] == "openai"
    assert back.data["model"]["extra"]["ds"]["protocol"] == "anthropic"
    # 脱敏
    assert back.masked()["model"]["extra"]["kimi"]["api_key"] == "***"
    # load_config 解析进 AppConfig.extra
    cfg = AppConfig(extra={k: ModelEndpoint(**v) for k, v in back.data["model"]["extra"].items()})
    assert set(cfg.extra) == {"kimi", "ds"}


async def test_set_model_extra_role_and_remove(tmp_path):
    store = ConfigStore(tmp_path / "c.toml")
    ctx = ToolContext(workspace=None, background=BackgroundRegistry(), emit=lambda e: None, fetch_output=lambda *_: "")
    res = await SetModelTool(store).run(
        {"role": "extra:kimi", "protocol": "openai", "base_url": "https://x", "api_key": "sk-1", "model": "k2"}, ctx
    )
    assert res.ok, res.error
    assert store.data["model"]["extra"]["kimi"]["model"] == "k2"
    res = await RemoveModelEndpointTool(store).run({"name": "kimi"}, ctx)
    assert res.ok
    assert "kimi" not in store.data["model"]["extra"]
    res = await RemoveModelEndpointTool(store).run({"name": "ghost"}, ctx)
    assert not res.ok
    ctx.facts.close() if ctx.facts else None


def test_plan_race_engines_truncate_and_skip(tmp_path):
    cfg = AppConfig(
        extra={
            "a": ModelEndpoint(model="a1"),
            "b": ModelEndpoint(model="b1"),
            "c": ModelEndpoint(model="c1"),
        }
    )
    cfg.agent.max_race_engines = 2
    specs, notes = plan_race_engines(cfg, claude_path="/usr/local/bin/claude")
    # 显式提供 runtime 内路径；上限 2 截断
    assert len(specs) <= 2
    assert any("截断" in n for n in notes)
    # 所选 Claude 执行环境没有 CLI 时跳过并说明
    cfg2 = AppConfig()
    cfg2.engines.claude_code.enabled = True
    specs2, notes2 = plan_race_engines(cfg2, claude_path="")
    assert specs2 == [] and any("未找到 claude" in n and "跳过" in n for n in notes2)
    # engines.enabled=false → 空
    cfg3 = AppConfig()
    cfg3.engines.enabled = False
    assert plan_race_engines(cfg3, claude_path="/x/claude") == ([], [])


def test_engine_display_names():
    cfg = AppConfig(extra={"ds": ModelEndpoint(model="deepseek-v4-flash")})
    cfg.engines.claude_code.enabled = True
    specs, _notes = plan_race_engines(cfg, claude_path="C:/bin/claude.cmd")
    assert [spec.display_name for spec in specs] == ["deepseek-v4-flash", "Claude Code"]


async def test_setting_imports_only_whitelisted_claude_config_names(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "super-secret",
                    "ANTHROPIC_BASE_URL": "https://gateway.example",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "sonnet-proxy",
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
                    "UNRELATED_SECRET": "must-not-import",
                },
                "permissions": {"allow": ["Bash(*)"]},
            }
        ),
        encoding="utf-8",
    )
    selected = load_claude_code_env(settings)
    assert selected["ANTHROPIC_AUTH_TOKEN"] == "super-secret"
    assert "UNRELATED_SECRET" not in selected

    store = ConfigStore(tmp_path / "config.toml")
    ctx = ToolContext(workspace=None, background=BackgroundRegistry(), emit=lambda e: None, fetch_output=lambda *_: "")
    result = await ConfigureClaudeCodeTool(store, settings).run({}, ctx)
    assert result.ok
    assert "ANTHROPIC_AUTH_TOKEN" in result.output
    assert "super-secret" not in result.output
    assert store.data["engines"]["claude_code"] == {
        "enabled": True,
        "containerized": True,
        "import_host_config": True,
    }
    assert "super-secret" not in store.path.read_text(encoding="utf-8")
    if ctx.facts:
        ctx.facts.close()


def test_claude_config_requires_auth(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://x"}}', encoding="utf-8")
    with pytest.raises(ClaudeConfigError, match="缺少"):
        load_claude_code_env(settings)


# ------------------------------------------------------------ 任务 2：race 扇出
def _stack(tmp_path, run_id):
    ws = Workspace(base=tmp_path, run_id=run_id)
    (ws.work_dir / "flag.txt").write_text(f"目标输出: {FLAG}\n", encoding="utf-8")
    events: list = []
    registry = ToolRegistry()
    register_builtin_tools(registry)
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws, background=background, emit=events.append,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l), flag_gate=gate,
    )
    return ws, events, registry, gate, background, ctx


async def test_race_engines_first_valid_wins(tmp_path):
    """引擎 A 闲逛（sleep），引擎 B 第二轮 submit flag → race 结束、A 被取消、exit 0。"""
    ws, events, registry, gate, background, ctx = _stack(tmp_path, "race-engines")
    read_cmd = "type flag.txt" if sys.platform == "win32" else "cat flag.txt"

    # 主 solver（闲逛）
    main_provider = FakeProvider(script=[("tool", "shell_exec", {"command": "echo 侦察中"})], repeat=True)
    factory_main = WorkerFactory(main_provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=5)
    main_loop = factory_main.make_loop("solver")
    main_goal = GoalMode(main_loop, Budget(max_minutes=5))
    main_history = [system_message("sys"), user_message("解题")]

    # 引擎 A（闲逛，10s sleep）
    sleep_provider = FakeProvider(script=[("tool", "shell_exec", {"command": f"{sys.executable} -c \"import time;time.sleep(10)\""})], repeat=True)
    fa = WorkerFactory(sleep_provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=5)
    goal_a = GoalMode(fa.make_loop("engine-a"), Budget(max_minutes=5))
    hist_a = [system_message("sys"), user_message("解题（引擎 A）")]

    # 引擎 B（第二轮 submit flag）
    b_provider = FakeProvider(script=[
        ("tool", "shell_exec", {"command": "echo 看看"}),
        ("tool", "shell_exec", {"command": read_cmd}),
        ("tool", "submit_flag", {"flag": FLAG}),
        ("text", "B 完成"),
    ])
    fb = WorkerFactory(b_provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=6)
    goal_b = GoalMode(fb.make_loop("engine-b"), Budget(max_minutes=5))
    hist_b = [system_message("sys"), user_message("解题（引擎 B）")]

    coordinator = Coordinator(
        board=ctx.facts, planner=ReasonPlanner(main_provider), factory=factory_main,
        emit=events.append, rollout=None, gate=gate, objective="race 测试",
        budget=Budget(max_minutes=5), workers=3, race_goal=main_goal, race_minutes=3.0,
        tick=0.05, planner_cooldown=0.1,
        race_engines=[
            ("engine-a", lambda: goal_a.solve(hist_a), goal_a),
            ("engine-b", lambda: goal_b.solve(hist_b), goal_b),
        ],
    )
    started = time.monotonic()
    solve_task = asyncio.create_task(coordinator.solve())
    active_deadline = time.monotonic() + 3
    while not coordinator.active_race_engines and time.monotonic() < active_deadline:
        await asyncio.sleep(0.01)
    assert coordinator.active_race_engines == ("solver", "engine-a", "engine-b")
    code = await solve_task
    elapsed = time.monotonic() - started
    await background.stop_all()

    assert code == 0
    assert gate.solved and gate.flag == FLAG
    assert coordinator.active_race_engines == ()
    assert elapsed < 8.0, f"race 应在 B 过闸后立即收尾，耗时 {elapsed:.1f}s（A 的 10s sleep 应被取消）"
    # 事件带引擎前缀 + 启动/结束可见
    notes = [e.message for e in events if getattr(e, "kind", "") == "swarm_note"]
    assert any("race 引擎启动：engine-b" in n for n in notes)
    assert any("取消其余" in n or "终止" in n for n in notes)
    engine_events = [e for e in events if getattr(e, "worker", None) in ("engine-a", "engine-b")]
    assert engine_events, "引擎事件缺 worker 前缀"
    ctx.facts.close()


# ------------------------------------------------------------ 任务 3：CLI 引擎收割
FAKE_CLAUDE = '''\
import sys, time
print('{"type":"system","subtype":"init"}', flush=True)
print('{"type":"assistant","message":{"content":[{"type":"text","text":"侦察中"}]}}', flush=True)
time.sleep(0.1)
print("FOUND_FLAG=flag{cli_engine_marker_ok}", flush=True)
time.sleep(30)  # 收割后应被取消（验证 terminate）
'''


async def test_cli_engine_harvest_and_cancel(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cli-engine")
    events: list = []
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    engine = CliEngine(
        "engine-claude",
        sys.executable,
        ws,
        gate,
        events.append,
        extra_args=[str(script)],
        total_timeout=60,
    )
    task = asyncio.create_task(engine.run("测试题", "http://t", r"flag\{[^{}]*\}"))
    # 等过闸
    deadline = time.monotonic() + 15
    while not gate.solved and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert gate.solved and gate.flag == "flag{cli_engine_marker_ok}"
    await asyncio.wait_for(task, timeout=5)
    # 进程被取消（不是等 30s sleep 结束）
    assert engine._proc is None or engine._proc.returncode is not None
    # CAS 归档含引擎完整输出（溯源证据）
    cas_texts = [p.read_text(encoding="utf-8", errors="replace") for p in ws.cas_files()]
    assert any("FOUND_FLAG=flag{cli_engine_marker_ok}" in t for t in cas_texts)
    notes = [e.message for e in events if getattr(e, "kind", "") == "swarm_note"]
    assert any("命中 flag" in n for n in notes)


async def test_cli_engine_runs_inside_runtime_with_injected_env(tmp_path):
    class StreamingRuntime:
        kind = "docker"

        def __init__(self) -> None:
            self.calls: list[tuple[list[str], dict]] = []
            self.cancelled = False

        async def run_argv(self, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            kwargs["on_output"]('{"type":"assistant","text":"FOUND_')
            kwargs["on_output"]('FLAG=flag{runtime_claude_ok}"}\n')
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    ws = Workspace(base=tmp_path, run_id="runtime-cli-engine")
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    runtime = StreamingRuntime()
    engine = CliEngine(
        "engine-claude",
        "/usr/local/bin/claude",
        ws,
        gate,
        lambda _event: None,
        runtime=runtime,
        env={"ANTHROPIC_AUTH_TOKEN": "secret", "ANTHROPIC_BASE_URL": "https://gateway"},
        total_timeout=30,
    )

    await asyncio.wait_for(engine.run("测试题", None, r"flag\{[^{}]*\}"), timeout=5)

    assert gate.solved and gate.flag == "flag{runtime_claude_ok}"
    assert runtime.cancelled
    argv, kwargs = runtime.calls[0]
    assert argv[0] == "/usr/local/bin/claude"
    assert "--verbose" in argv
    assert "--safe-mode" in argv
    assert "--no-session-persistence" in argv
    assert "--dangerously-skip-permissions" in argv
    assert kwargs["cwd"] == ws.work_dir / "engine-claude"
    assert kwargs["env"]["ANTHROPIC_AUTH_TOKEN"] == "secret"


async def test_cli_engine_fallback_regex_scan(tmp_path):
    """无 marker 时兜底 flag_format 正则扫输出。"""
    ws = Workspace(base=tmp_path, run_id="cli-engine-fallback")
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    script = tmp_path / "fake2.py"
    script.write_text('print("扫描结果里直接有 flag{fallback_regex_ok} 这东西")\n', encoding="utf-8")
    engine = CliEngine("engine-claude", sys.executable, ws, gate, lambda e: None, extra_args=[str(script)], total_timeout=30)
    await engine.run("题", None, r"flag\{[^{}]*\}")
    assert gate.solved and gate.flag == "flag{fallback_regex_ok}"


async def test_cli_engine_rejects_bad_flag(tmp_path):
    """引擎报占位符/格式不符的 flag → 闸门拒收，引擎继续跑。"""
    ws = Workspace(base=tmp_path, run_id="cli-engine-reject")
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    script = tmp_path / "fake3.py"
    script.write_text('print("FOUND_FLAG=flag{...}")\n', encoding="utf-8")
    engine = CliEngine("engine-claude", sys.executable, ws, gate, lambda e: None, extra_args=[str(script)], total_timeout=30)
    await engine.run("题", None, r"flag\{[^{}]*\}")
    assert not gate.solved  # 占位符被四重校验拒收
