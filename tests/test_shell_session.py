"""shell_session：开会话 → echo hello → read 拿到 hello → cd 后状态保持。"""
from __future__ import annotations

import asyncio
import sys

from foxdog.executor.background import BackgroundRegistry
from foxdog.executor.process import IS_WINDOWS
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext
from foxdog.tools.builtin.session import ShellSessionTool

PY = f'"{sys.executable}"' if " " in sys.executable else sys.executable


async def _read_until(tool: ShellSessionTool, ctx: ToolContext, sid: str, needle: str, timeout: float = 15.0) -> str:
    """循环 read 直到看到 needle（shell 启动/回显有延迟）。"""
    out = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while needle not in out and loop.time() < deadline:
        res = await tool.run({"action": "read", "session_id": sid, "wait_ms": 500}, ctx)
        assert res.ok, res.error
        out += res.output
    return out


async def test_session_echo_and_state(tmp_path):
    ws = Workspace(base=tmp_path, run_id="session-test")
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )
    tool = ShellSessionTool()
    sid = "s1"
    try:
        res = await tool.run({"action": "open", "session_id": sid}, ctx)
        assert res.ok, res.error
        assert "已建立" in res.output

        # echo hello → read 拿到 hello
        res = await tool.run({"action": "write", "session_id": sid, "data": "echo hello-marker-42"}, ctx)
        assert res.ok, res.error
        out = res.output
        if "hello-marker-42" not in out:
            out += await _read_until(tool, ctx, sid, "hello-marker-42")
        assert "hello-marker-42" in out

        # 状态保持：cd 进子目录后 pwd/cd 显示新路径
        # 注意：write 动作本身会等待 wait_ms 并带回新输出，要一并拼上
        sub = ws.work_dir / "subdir"
        sub.mkdir()
        if IS_WINDOWS:
            quoted = str(sub).replace("'", "''")
            res = await tool.run(
                {"action": "write", "session_id": sid, "data": f"Set-Location -LiteralPath '{quoted}'"},
                ctx,
            )
            res = await tool.run({"action": "write", "session_id": sid, "data": "Get-Location"}, ctx)
        else:
            res = await tool.run({"action": "write", "session_id": sid, "data": f'cd "{sub}"'}, ctx)
            out = res.output + await _read_until(tool, ctx, sid, "subdir")
            res = await tool.run({"action": "write", "session_id": sid, "data": "pwd"}, ctx)
        out = res.output
        if "subdir" not in out:
            out += await _read_until(tool, ctx, sid, "subdir")
        assert "subdir" in out.replace("\\", "/")

        if IS_WINDOWS:
            expected = "中文输出"
            command = f"Write-Output '{expected}'"
            res = await tool.run({"action": "write", "session_id": sid, "data": command}, ctx)
            out = res.output
            if expected not in out:
                out += await _read_until(tool, ctx, sid, expected)
            assert expected in out

        # read 非阻塞：没有新输出时返回提示
        res = await tool.run({"action": "read", "session_id": sid, "wait_ms": 300}, ctx)
        assert res.ok
    finally:
        res = await tool.run({"action": "close", "session_id": sid}, ctx)
        assert res.ok, res.error
        assert "已关闭" in res.output
    assert ctx.sessions.ids() == []


async def test_session_errors(tmp_path):
    ws = Workspace(base=tmp_path, run_id="session-err")
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )
    tool = ShellSessionTool()
    # 未 open 先 write/read/close
    for action in ("write", "read", "close"):
        args = {"action": action, "session_id": "ghost"}
        if action == "write":
            args["data"] = "echo x"
        res = await tool.run(args, ctx)
        assert not res.ok and "未知会话" in res.error
    # 重复 open
    res = await tool.run({"action": "open", "session_id": "dup"}, ctx)
    assert res.ok
    res = await tool.run({"action": "open", "session_id": "dup"}, ctx)
    assert not res.ok and "已存在" in res.error
    await ctx.sessions.close_all()
