"""统一平台：ConfigStore 读写、/model 引导配置、/setting 对话配置、命令补全、平台自动提交、/fetch。"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import httpx

from foxdog import runner as runner_mod
from foxdog.config import ConfigStore, ModelEndpoint
from foxdog.interactive import ChatApp, CommandInput
from foxdog.llm.client import FakeProvider

FLAG = "flag{smoke_test_flag_123}"


def _submit(app: ChatApp, text: str) -> None:
    app.on_input_submitted(type("E", (), {"value": text, "input": app.query_one(CommandInput)})())


def _timeline(app: ChatApp) -> str:
    """文本聚合：timeline + setting-full（/setting overlay 打开时事件路由到那里）。"""
    from rich.text import Text

    parts = []
    for sel in ("#timeline Static", "#setting-full Static"):
        for w in app.query(sel):
            content = w.render()
            parts.append(content.plain if isinstance(content, Text) else str(content))
    return "\n".join(parts)


async def _wait_idle(app: ChatApp, pilot, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.2)
        task = app._solve_task
        if not app.busy and (task is None or task.done()):
            return
    raise AssertionError("超时未空闲")


# ------------------------------------------------------------ ConfigStore
def test_config_store_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    store = ConfigStore(path)
    store.set_model("solver", protocol="anthropic", base_url="https://api.anthropic.com", api_key="sk-x", model="claude-x")
    store.set_platform(fetch_url="http://x/api/{id}", submit_url="http://x/submit", default_workers=6)
    store.add_mcp_server("kb", command="python", args=["-m", "kb"])
    store.set_agent(mode="plan", budget_minutes=45)
    store.save()

    back = ConfigStore(path)  # 重读
    assert back.data["model"]["solver"]["protocol"] == "anthropic"
    assert back.data["platform"]["default_workers"] == 6
    assert back.data["mcp_servers"]["kb"]["args"] == ["-m", "kb"]
    assert back.data["budget"]["max_minutes"] == 45
    assert back.masked()["model"]["solver"]["api_key"] == "***"
    if sys.platform != "win32":  # Windows 只有只读位语义，0600 断言只对 POSIX
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # 覆盖写保留已有字段
    back.set_platform(default_workers=8)
    back.save()
    again = ConfigStore(path)
    assert again.data["platform"]["fetch_url"] == "http://x/api/{id}"
    assert again.data["platform"]["default_workers"] == 8


# ------------------------------------------------------------ /model 引导式配置
async def test_model_guided_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    config_path = tmp_path / "config.toml"
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=config_path)
    app.test_endpoint = lambda ep: asyncio.sleep(0, (True, "连接正常"))  # 不打真网络
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/model")
        await pilot.pause(0.2)
        assert "开始配置模型" in _timeline(app)
        _submit(app, "openai")
        await pilot.pause(0.2)
        assert "base_url" in _timeline(app)
        _submit(app, "https://api.test/v1")
        await pilot.pause(0.2)
        # api_key 步骤：输入框进入 password 模式（不回显）
        assert app.query_one(CommandInput).password is True
        _submit(app, "sk-secret-key")
        await pilot.pause(0.2)
        assert app.query_one(CommandInput).password is False
        assert "sk-secret-key" not in _timeline(app)  # 时间线不回显 key
        _submit(app, "test-model-x")
        await pilot.pause(0.5)
        text = _timeline(app)
        assert "已配置并保存" in text
        # 落盘 + 热切换
        data = json.loads(json.dumps(ConfigStore(config_path).data))
        solver = data["model"]["solver"]
        assert solver["model"] == "test-model-x" and solver["protocol"] == "openai"
        assert solver["api_key"] == "sk-secret-key"  # key 只出现在文件里
        assert app.session.cfg.solver.model == "test-model-x"
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_model_setup_connection_failure_not_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    config_path = tmp_path / "config.toml"
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=config_path)
    app.test_endpoint = lambda ep: asyncio.sleep(0, (False, "ConnectionError: 拒连"))
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/model")
        for answer in ("anthropic", "-", "sk-x", "claude-test"):
            _submit(app, answer)
            await pilot.pause(0.2)
        await pilot.pause(0.5)
        assert "连接测试失败" in _timeline(app)
        assert not config_path.is_file()  # 失败不落盘
        _submit(app, "/quit")
        await pilot.pause(0.5)


# ------------------------------------------------------------ /setting 对话式配置
async def test_setting_conversational(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    config_path = tmp_path / "config.toml"
    script = [
        ("tool", "set_platform", {
            "fetch_url": "http://x/api/chal/{id}",
            "submit_url": "http://x/api/submit",
            "default_workers": 6,
        }),
        ("text", "已配好：题目从 http://x/api/chal 拉，flag 提交到 http://x/api/submit，worker 数 6"),
    ]
    real_build_session = runner_mod.build_session

    async def patched(**kwargs):
        session = await real_build_session(**kwargs)
        session.provider = FakeProvider(script=script)
        return session

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=config_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/setting 题目从 http://x/api/chal 拉，flag 提交到 http://x/api/submit，worker 数改 6")
        await _wait_idle(app, pilot)
        # 配置落盘
        data = ConfigStore(config_path).data
        assert data["platform"]["fetch_url"] == "http://x/api/chal/{id}"
        assert data["platform"]["submit_url"] == "http://x/api/submit"
        assert data["platform"]["default_workers"] == 6
        # 时间线有确认文本
        assert "已配好" in _timeline(app)
        # 会话热加载
        assert app.session.cfg.platform.submit_url == "http://x/api/submit"
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_setting_requires_model(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        # 伪造未配置状态
        app.session.cfg.solver.provider = "openai-compatible"
        app.session.cfg.solver.base_url = None
        _submit(app, "/setting 改点啥")
        await pilot.pause(0.2)
        assert "先 /model" in _timeline(app)
        _submit(app, "/quit")
        await pilot.pause(0.5)


# ------------------------------------------------------------ 命令自动补全
async def test_autocomplete_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        # 输入 /mo → 弹层含 /model /mode
        inp.value = "/mo"
        await pilot.pause(0.3)
        assert app._ac_visible
        names = app._ac_names()
        assert "/model" in names and "/mode" in names
        # 选中补全（首个候选是 /model）
        app._ac_accept()
        await pilot.pause(0.2)
        assert inp.value == "/model "
        assert not app._ac_visible
        # 非 / 开头不出现
        inp.value = "普通文本"
        await pilot.pause(0.3)
        assert not app._ac_visible
        # Esc 关闭
        inp.value = "/se"
        await pilot.pause(0.3)
        assert app._ac_visible and "/setting" in app._ac_names()
        app._ac_hide()
        assert not app._ac_visible
        _submit(app, "/quit")
        await pilot.pause(0.5)


# ------------------------------------------------------------ 平台自动提交
class _RecordingResp:
    status_code = 200
    text = '{"ok":true}'


class _RecordingClient:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, json=None, **kw):
        type(self).calls.append({"url": url, "json": json})
        return _RecordingResp()

    async def get(self, url, **kw):
        type(self).calls.append({"url": url})
        return _RecordingResp()


async def test_platform_auto_submit(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    events: list = []
    run = await runner_mod.build_run(
        "smoke",
        None,
        r"flag\{[^{}]*\}",
        [],
        "",
        None, None, None, 1, None,
        ["platform.submit_url=http://platform.local/api/submit", "runtime.kind=local"],
        emit=events.append,
        workspace=tmp_path,
    )
    code = await runner_mod.execute_run(run)
    assert code == 0
    posts = [c for c in _RecordingClient.calls if "json" in c]
    assert len(posts) == 1
    assert posts[0]["url"] == "http://platform.local/api/submit"
    assert posts[0]["json"]["flag"] == FLAG
    assert posts[0]["json"]["run_id"] == run.ws.run_id
    # 事件流报告了提交结果
    assert any(getattr(e, "kind", "") == "swarm_note" and "已自动提交平台" in e.message for e in events)


# ------------------------------------------------------------ /fetch
async def test_fetch_with_platform_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.session.cfg.platform.fetch_url = "http://x/api/chal/{id}"
        _submit(app, "/fetch 42")
        await pilot.pause(0.5)
        assert _RecordingClient.calls[0]["url"] == "http://x/api/chal/42"  # {id} 替换
        assert "题目已拉取到输入框" in _timeline(app)
        assert app.query_one(CommandInput).value  # 内容进了输入框
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_fetch_fallback_plain_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/fetch http://y/chal/7")  # 未配置 fetch_url：参数当 URL
        await pilot.pause(0.5)
        assert _RecordingClient.calls[0]["url"] == "http://y/chal/7"
        assert "题目已拉取到输入框" in _timeline(app)
        _submit(app, "/quit")
        await pilot.pause(0.5)


# ------------------------------------------------------------ 平台监控
def test_parse_platform_stats_aggregate_and_list():
    from foxdog.platform import parse_platform_stats

    agg = parse_platform_stats({"total": 10, "solved": 3})
    assert agg["total"] == 10 and agg["solved"] == 3

    listed = parse_platform_stats({"data": {"challenges": [
        {"id": "c1", "title": "Web 1", "solved": True},
        {"id": "c2", "title": "Pwn 1", "status": "open"},
    ]}})
    assert listed["total"] == 2 and listed["solved"] == 1
    assert [c["title"] for c in listed["challenges"]] == ["Web 1", "Pwn 1"]


def test_platform_render_is_keyerror_safe():
    """_render 对未知占位符原样保留，不因配置里意外花括号崩溃。"""
    from foxdog.platform import _render

    out = _render("path/{id}/{unknown}", {"id": "42"})
    assert out == "path/42/{unknown}"
    nested = _render({"q": "{id}", "extra": "{missing}"}, {"id": "7"})
    assert nested == {"q": "7", "extra": "{missing}"}


async def test_platform_poll_updates_counts(tmp_path, monkeypatch):
    """配置 status_url + poll_seconds → 轮询后主界面显示题目计数，监控面板可见。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")

    async def fake_poll(cfg, **_kw):
        return {"total": 8, "solved": 2, "challenges": [
            {"id": "c1", "title": "Web", "solved": True},
            {"id": "c2", "title": "Crypto", "solved": False},
        ]}

    monkeypatch.setattr("foxdog.platform.poll_platform_status", fake_poll)
    app = ChatApp(
        workspace=tmp_path,
        overrides=[
            "runtime.kind=local",
            "platform.status_url=http://p/api/status",
            "platform.poll_seconds=5",
        ],
        config_path=tmp_path / "c.toml",
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.6)
        # 启动即拉一次；等轮询任务完成
        deadline = time.monotonic() + 5
        while app._platform_stats.get("total") is None and time.monotonic() < deadline:
            await pilot.pause(0.2)
        assert app._platform_stats["total"] == 8
        assert app._platform_monitor is True
        from rich.text import Text
        status = app.query_one("#status").render()
        status_plain = status.plain if isinstance(status, Text) else str(status)
        assert "2/8" in status_plain
        # 监控面板可见且含题目
        panel = app.query_one("#monitor")
        assert str(panel.styles.display) == "block"
        _submit(app, "/quit")
        await pilot.pause(0.3)
