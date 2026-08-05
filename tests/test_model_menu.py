"""任务 1/2：/model 菜单化（协议菜单、Kimi 预设、Esc 取消）+ /setting 独立页多轮对话。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from textual.widgets import OptionList

from foxdog.config import ConfigStore
from foxdog.interactive import ChatApp, CommandInput, _PRESET_OPTIONS, _PROTOCOL_OPTIONS
from foxdog.llm.client import FakeProvider
from foxdog import runner as runner_mod


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    return ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")


def _submit(app: ChatApp, text: str) -> None:
    app.on_input_submitted(type("E", (), {"value": text, "input": app.query_one(CommandInput)})())


def _timeline(app: ChatApp) -> str:
    from rich.text import Text

    parts = []
    for w in app.query("#timeline Static"):
        content = w.render()
        parts.append(content.plain if isinstance(content, Text) else str(content))
    return "\n".join(parts)


# ------------------------------------------------------------ /model 菜单
async def test_model_menu_flow_with_preset(tmp_path, monkeypatch):
    """菜单选协议 → 菜单选 Kimi 预设 → key → 模型默认 → 落盘热切换。"""
    config_path = tmp_path / "c.toml"
    app = _app(tmp_path, monkeypatch)
    app.test_endpoint = lambda ep: asyncio.sleep(0, (True, "ok"))
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/model")
        await pilot.pause(0.3)
        # 1. 协议菜单可见，三项
        menu = app.query_one("#menu", OptionList)
        assert app._menu_active and len(menu.options) == 3
        ids = [str(o.id) for o in menu.options]
        assert ids == ["openai", "responses", "anthropic"]
        # 选 anthropic（↓ 两次回车）
        app._menu_move(1)
        app._menu_move(1)
        app._menu_select()
        await pilot.pause(0.2)
        assert app._setup["answers"]["protocol"] == "anthropic"
        # 2. 预设菜单：选 Kimi Code
        ids2 = [str(o.id) for o in menu.options]
        assert "kimi-code" in ids2 and "kimi-open" in ids2 and "manual" in ids2
        app._menu_select_by = None
        # 高亮 kimi-code 选中
        idx = ids2.index("kimi-code")
        for _ in range(idx):
            app._menu_move(1)
        app._menu_select()
        await pilot.pause(0.2)
        answers = app._setup["answers"]
        assert answers["protocol"] == "anthropic"
        assert answers["base_url"] == "https://api.kimi.com/coding/"
        assert answers["model"] == "kimi-for-coding"
        # 3. api_key 步骤（password 模式）
        assert app.query_one(CommandInput).password is True
        _submit(app, "sk-secret")
        await pilot.pause(0.2)
        assert "sk-secret" not in _timeline(app)
        # 4. 模型名：回车用预设默认
        _submit(app, "")
        await pilot.pause(0.2)
        # 空提交被 on_input_submitted 忽略 → 手工推进空值
        app._setup_input("")
        await pilot.pause(0.5)
        data = ConfigStore(config_path).data
        solver = data["model"]["solver"]
        assert solver["protocol"] == "anthropic"
        assert solver["base_url"] == "https://api.kimi.com/coding/"
        assert solver["model"] == "kimi-for-coding"
        assert solver["api_key"] == "sk-secret"
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_model_menu_esc_cancels_anywhere(tmp_path, monkeypatch):
    """Esc 在任意 setup 步取消整个 /model（不再依赖 /cancel）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/model")
        await pilot.pause(0.2)
        assert app._setup is not None and app._menu_active
        # Esc（经 CommandInput 按键路由）
        inp = app.query_one(CommandInput)
        inp.focus()
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app._setup is None and not app._menu_active
        assert "已取消配置流程" in _timeline(app)
        # 输入步也 Esc：重新进 → 选 openai → manual → base_url 步 Esc
        _submit(app, "/model")
        await pilot.pause(0.2)
        app._menu_select()  # openai
        await pilot.pause(0.2)
        menu = app.query_one("#menu", OptionList)
        idx = [str(o.id) for o in menu.options].index("manual")
        for _ in range(idx):
            app._menu_move(1)
        app._menu_select()  # manual → base_url 输入步
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app._setup is None and "已取消配置流程" in _timeline(app)
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_model_menu_manual_base_url(tmp_path, monkeypatch):
    """manual 预设 → 手填 base_url → 全流程。"""
    config_path = tmp_path / "c.toml"
    app = _app(tmp_path, monkeypatch)
    app.test_endpoint = lambda ep: asyncio.sleep(0, (True, "ok"))
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/model")
        await pilot.pause(0.2)
        app._menu_select()  # openai
        await pilot.pause(0.2)
        menu = app.query_one("#menu", OptionList)
        idx = [str(o.id) for o in menu.options].index("manual")
        for _ in range(idx):
            app._menu_move(1)
        app._menu_select()
        await pilot.pause(0.2)
        app._setup_input("https://api.custom.dev/v1")
        await pilot.pause(0.2)
        app._setup_input("sk-x")
        await pilot.pause(0.2)
        app._setup_input("custom-model-1")
        await pilot.pause(0.5)
        solver = ConfigStore(config_path).data["model"]["solver"]
        assert solver["base_url"] == "https://api.custom.dev/v1" and solver["model"] == "custom-model-1"
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ /setting 独立页
async def test_setting_page_multiturn_and_reentry(tmp_path, monkeypatch):
    """开 /setting 页 → 发两轮需求 → Esc 返回 → 重进历史仍在。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    script = [
        ("text", "已配置平台地址"),
        ("text", "worker 数已改 6"),
    ]
    real_build_session = runner_mod.build_session

    async def patched(**kwargs):
        session = await real_build_session(**kwargs)
        session.provider = FakeProvider(script=script)
        return session

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        # 进入 setting 页（无参也进）
        _submit(app, "/setting")
        await pilot.pause(0.3)
        assert app._setting_page
        assert app.query_one("#setting-full").styles.display == "block"
        # 输入区还在（bug 修复点：此前连输入框一起隐藏）
        assert app.query_one("#bottom").styles.display != "none"
        async def wait_copilot():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                await pilot.pause(0.2)
                t = getattr(app, "_solve_task_copilot", None)
                if t is not None and t.done() and not app._setting_busy:
                    return
            raise AssertionError("copilot 未在限时内完成")

        # 第一轮需求
        _submit(app, "把平台 flag 提交地址配上")
        await wait_copilot()
        # 第二轮需求（多轮连续：history 累积）
        _submit(app, "worker 数改 6")
        await wait_copilot()
        # agent 流式输出在 setting-full 内挂载（AgentStreamBlock 正文也在其中），全量收集
        from rich.text import Text as _T

        page_text = "\n".join(
            (w.render().plain if isinstance(w.render(), _T) else str(w.render()))
            for w in app.query("#setting-full Static")
        )
        assert "已配置平台地址" in page_text and "worker 数已改 6" in page_text
        # 多轮历史在 session 级
        user_msgs = [m for m in app._setting_history if m.get("role") == "user"]
        assert len(user_msgs) == 2
        # Esc 返回
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not app._setting_page
        # 重进：页面再开，历史仍在（session 级 _setting_history 未清）
        _submit(app, "/setting")
        await pilot.pause(0.3)
        assert app._setting_page
        user_msgs = [m for m in app._setting_history if m.get("role") == "user"]
        assert len(user_msgs) == 2, "重进后 setting 历史丢失"
        _submit(app, "/quit")
        await pilot.pause(0.3)


def test_preset_table_integrity():
    """预设表本身：协议/base_url 与官方文档一致（防手滑写错）。"""
    presets = {p[0]: p[2] for p in _PRESET_OPTIONS if p[2]}
    assert presets["kimi-open"]["base_url"] == "https://api.moonshot.cn/v1"
    assert presets["kimi-open"]["protocol"] == "openai"
    assert presets["kimi-code"]["base_url"] == "https://api.kimi.com/coding/"
    assert presets["kimi-code"]["protocol"] == "anthropic"
    assert presets["deepseek-open"]["base_url"] == "https://api.deepseek.com"
    assert presets["deepseek-anth"]["base_url"] == "https://api.deepseek.com/anthropic"
    assert [v for v, _ in _PROTOCOL_OPTIONS] == ["openai", "responses", "anthropic"]


def test_setting_api_key_is_replaced_before_model_sees_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    raw_key = "sk-this-must-never-enter-model-history"
    safe, shown = app._protect_setting_secrets(f"帮我配置，api_key: {raw_key}")
    assert raw_key not in safe
    assert raw_key not in shown
    assert "secret://setting/" in safe
    assert "***" in shown
    handle = next(part for part in safe.split() if part.startswith("secret://setting/"))
    assert app._setting_secrets.resolve(handle) == raw_key


async def test_model_show_lists_configured_endpoints(tmp_path, monkeypatch):
    """/model show：展示已配置端点及状态，api_key 不回显真值。"""
    app = _app(tmp_path, monkeypatch)
    app.store.set_model(
        "solver", provider="openai-compatible", protocol="anthropic",
        base_url="https://api.kimi.com/coding/", api_key="sk-secret-xyz", model="kimi-for-coding",
    )
    app.store.save()
    async with app.run_test() as pilot:
        await pilot.pause(0.4)
        _submit(app, "/model show")
        await pilot.pause(0.3)
        tl = _timeline(app)
        assert "已配置模型" in tl
        assert "kimi-for-coding" in tl
        assert "有 key" in tl
        assert "sk-secret-xyz" not in tl  # 真值不回显
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_model_step_tab_completion_from_fetched_ids(tmp_path, monkeypatch):
    """/model 模型名步：拉取到候选后按前缀 tab 补全；拉不到无补全。"""
    async def fake_fetch(ep, **_kw):
        return ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash"]

    monkeypatch.setattr("foxdog.llm.model_catalog.fetch_model_ids", fake_fetch)
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.4)
        # 直接构造到模型名步：模拟拉取结果
        app._setup = {"step": 4, "answers": {"protocol": "openai"}}
        app._setup_models = ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash"]
        # 输入前缀触发补全
        inp = app.query_one(CommandInput)
        inp.value = "deepseek-r"
        app._ac_show_models("deepseek-r")
        await pilot.pause(0.2)
        assert app._ac_names() == ["deepseek-reasoner"]
        # 空拉取列表 → 无补全
        app._setup_models = []
        app._ac_show_models("x")
        assert app._ac_names() == []
        app._setup = None
        _submit(app, "/quit")
        await pilot.pause(0.3)
