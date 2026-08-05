"""子进程执行器：输出/exit code、空闲超时、大输出截断+spill。"""
from __future__ import annotations

import sys
from pathlib import Path

from foxdog.executor.process import (
    IS_WINDOWS,
    ProcessOutputDecoder,
    decode_process_output,
    run_shell,
    shell_argv,
)
from foxdog.store.workspace import Workspace

PY = f'"{sys.executable}"' if " " in sys.executable else sys.executable


async def test_echo_output_and_exit_code():
    res = await run_shell("echo hello")
    assert res.exit_code == 0
    assert "hello" in res.output
    assert not res.truncated and res.handle is None


def test_windows_shell_defaults_to_powershell_7():
    if not IS_WINDOWS:
        return
    argv = shell_argv("Write-Output ok")
    assert Path(argv[0]).stem.lower() == "pwsh"
    assert argv[-2:] == ["-Command", "Write-Output ok"]
    assert "-NoProfile" in argv and "-NonInteractive" in argv


async def test_windows_powershell_syntax():
    if not IS_WINDOWS:
        return
    res = await run_shell("Write-Output ('ps-major=' + $PSVersionTable.PSVersion.Major); Write-Output '中文正常'")
    assert res.exit_code == 0
    assert "ps-major=7" in res.output
    assert "中文正常" in res.output


async def test_nonzero_exit_code():
    res = await run_shell("exit 3")
    assert res.exit_code == 3


async def test_stderr_merged():
    cmd = f'{PY} -c "import sys; sys.stderr.write(\'err-out\\n\')"'
    res = await run_shell(cmd)
    assert res.exit_code == 0
    assert "err-out" in res.output


async def test_idle_timeout_kills():
    # 命令长时间无输出：空闲超时 1s 后应被 kill
    if IS_WINDOWS:
        cmd = "ping -n 10 127.0.0.1 >nul"
    else:
        cmd = "sleep 10"
    res = await run_shell(cmd, idle_timeout=1.0, total_timeout=30.0)
    assert res.idle_timed_out
    assert not res.timed_out


async def test_total_timeout():
    if IS_WINDOWS:
        cmd = "ping -n 10 127.0.0.1"  # 持续有输出，不吃空闲超时
    else:
        cmd = "for i in 1 2 3 4 5 6 7 8 9 10; do echo tick; sleep 1; done"
    res = await run_shell(cmd, idle_timeout=5.0, total_timeout=2.0)
    assert res.timed_out
    assert not res.idle_timed_out


async def test_large_output_truncated_and_spilled(tmp_path):
    ws = Workspace(base=tmp_path, run_id="spill")
    cmd = f'{PY} -c "print(\'A\'*200000)"'
    res = await run_shell(cmd, cas_store=ws.cas_store)
    assert res.exit_code == 0
    assert res.truncated
    assert len(res.output.encode()) <= 64 * 1024
    assert res.handle is not None
    full = ws.cas_fetch_text(res.handle)
    assert full.count("A") == 200000  # CAS 里是全文


def test_process_output_decoder_prefers_utf8_and_falls_back_to_cp936():
    expected = "中文测试"
    assert decode_process_output(expected.encode("utf-8"), ("cp936",)) == expected
    assert decode_process_output(expected.encode("cp936"), ("cp936",)) == expected


def test_process_output_decoder_preserves_utf8_character_across_chunks():
    raw = "前中文后".encode("utf-8")
    decoder = ProcessOutputDecoder(("cp936",))
    parts = [decoder.feed(raw[:4]), decoder.feed(raw[4:5]), decoder.feed(raw[5:]), decoder.feed(b"", final=True)]
    assert "".join(parts) == "前中文后"
    assert "�" not in "".join(parts)


def test_process_output_decoder_handles_cp936_chunks_and_mixed_segments():
    decoder = ProcessOutputDecoder(("cp936",))
    cp936 = "中文".encode("cp936")
    parts = [
        decoder.feed("UTF-8：".encode("utf-8")),
        decoder.feed(cp936[:1]),
        decoder.feed(cp936[1:3]),
        decoder.feed(cp936[3:] + b" done"),
        decoder.feed(b"", final=True),
    ]
    assert "".join(parts) == "UTF-8：中文 done"


async def test_cp936_output_is_normalized_before_cas_spill(tmp_path):
    ws = Workspace(base=tmp_path, run_id="cp936-spill")
    expected = "中文测试" * 100
    raw = expected.encode("cp936")
    if IS_WINDOWS:
        cmd = f'{PY} -c "import sys;sys.stdout.buffer.write({raw!r})"'
    else:
        cmd = f"{PY} -c 'import sys;sys.stdout.buffer.write({raw!r})'"
    res = await run_shell(cmd, max_output=16, cas_store=ws.cas_store)
    assert res.truncated and res.handle is not None
    assert ws.cas_fetch_text(res.handle) == expected
