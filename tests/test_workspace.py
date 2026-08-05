"""Workspace/CAS：存取、by-handle 取回、路径约束、winner。"""
from __future__ import annotations

import pytest

from foxdog.core.context import fetch_output, spill_text
from foxdog.store.workspace import Workspace
from foxdog.executor.background import BackgroundRegistry
from foxdog.tools import ToolContext
from foxdog.tools.builtin.files import EditFileTool, ReadFileTool, WriteFileTool


def test_cas_store_fetch_roundtrip(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cas")
    data = b"hello cas world" * 100
    handle = ws.cas_store(data)
    assert len(handle) == 64
    assert ws.cas_fetch(handle) == data
    # 内容寻址：同内容同句柄，不重复写
    assert ws.cas_store(data) == handle
    assert len(ws.cas_files()) == 1


def test_cas_fetch_offset_limit(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cas")
    handle = ws.cas_store(b"0123456789")
    assert ws.cas_fetch(handle, offset=2, limit=4) == b"2345"
    assert ws.cas_fetch_text(handle, offset=5) == "56789"


def test_fetch_output_tool_view(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cas")
    handle = ws.cas_store(b"A" * 10000)
    text = fetch_output(ws, handle, offset=0, limit=100)
    assert text.count("A") == 100
    assert "10000" in text  # footer 提示总大小
    assert fetch_output(ws, "f" * 64).startswith("error:")


def test_spill_text(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cas")
    small, handle, truncated = spill_text(ws, "short", threshold=100)
    assert not truncated and handle is None and small == "short"
    big, handle, truncated = spill_text(ws, "B" * 500, threshold=100)
    assert truncated and handle is not None
    assert "省略" in big
    assert ws.cas_fetch_text(handle) == "B" * 500


def test_resolve_restricts_to_workspace(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cas")
    p = ws.resolve("exploit.py")
    assert ws.work_dir.resolve() in p.parents or p.parent == ws.work_dir.resolve()
    with pytest.raises(PermissionError):
        ws.resolve("../../evil.py")
    with pytest.raises(PermissionError):
        ws.resolve(str(tmp_path / "outside.py"))


async def test_file_tools_allow_external_absolute_paths(tmp_path):
    ws = Workspace(base=tmp_path, run_id="external-files")
    external = tmp_path / "outside" / "payload.py"
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda _event: None,
        fetch_output=lambda _handle, _offset, _limit: "",
    )

    written = await WriteFileTool().run({"path": str(external), "content": "print('old')\n"}, ctx)
    assert written.ok and external.is_file()
    assert str(external.resolve()) in written.output

    read = await ReadFileTool().run({"path": str(external)}, ctx)
    assert read.ok and read.output == "print('old')\n"

    edited = await EditFileTool().run(
        {"path": str(external), "old": "old", "new": "new"},
        ctx,
    )
    assert edited.ok and external.read_text(encoding="utf-8") == "print('new')\n"
    assert str(external.resolve()) in edited.output


def test_file_path_relative_paths_still_use_run_workspace(tmp_path):
    ws = Workspace(base=tmp_path, run_id="relative-files")
    assert ws.resolve_file_path("nested/a.txt") == (ws.work_dir / "nested" / "a.txt").resolve()


def test_workspace_seed_from_run_copies_resumable_state(tmp_path):
    old = Workspace(base=tmp_path, run_id="old")
    (old.work_dir / "exploit.py").write_text("print(1)\n", encoding="utf-8")
    (old.inputs_dir / "chall.txt").write_text("input\n", encoding="utf-8")
    handle = old.cas_store(b"flag{from_old_output}")
    from foxdog.swarm.facts import FactsBoard

    board = FactsBoard(old.dir / "blackboard.db")
    board.post_fact("旧 run 已确认的事实", actor="worker-1")
    board.post_directive("旧 run 的控制指令", actor="spectator", priority="high")
    board.close()

    new = Workspace(base=tmp_path, run_id="new", seed_from=old.dir)
    assert (new.work_dir / "exploit.py").read_text(encoding="utf-8") == "print(1)\n"
    assert (new.inputs_dir / "chall.txt").read_text(encoding="utf-8") == "input\n"
    assert new.cas_fetch(handle) == b"flag{from_old_output}"

    restored = FactsBoard(new.dir / "blackboard.db")
    assert restored.facts()[0]["content"] == "旧 run 已确认的事实"
    assert restored.directives()[0]["content"] == "旧 run 的控制指令"
    restored.close()


def test_winner_roundtrip(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cas")
    assert ws.read_winner() is None
    ws.write_winner("flag{x}", reason="ok")
    assert ws.read_winner()["flag"] == "flag{x}"
