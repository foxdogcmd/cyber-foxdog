"""writeup 骨架：基于 rollout + facts.jsonl + winner.json 生成，成败都有。"""
from __future__ import annotations

import json
import time

from foxdog.store.writeup import generate_writeup


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def test_writeup_with_flag(tmp_path):
    run = tmp_path / "run1"
    run.mkdir()
    _write_jsonl(
        run / "rollout.jsonl",
        [
            {"type": "session_meta", "run_id": "run1", "objective": "一道文件写入题", "target": "http://t:8080",
             "flag_format": r"flag\{.*\}", "model": "fake", "provider": "fake", "ts": time.time()},
            {"type": "tool_call", "ts": time.time(), "calls": [{"id": "c1", "name": "http_request", "arguments": '{"url":"http://t:8080/"}'}]},
            {"type": "tool_call", "ts": time.time(), "calls": [{"id": "c2", "name": "shell_exec", "arguments": '{"command":"cat /flag"}'}]},
            {"type": "tool_call", "ts": time.time(), "calls": [{"id": "c3", "name": "submit_flag", "arguments": '{"flag":"flag{abc_123}"}'}]},
        ],
    )
    _write_jsonl(
        run / "facts.jsonl",
        [
            {"kind": "fact", "fact": "8080 是 nginx"},
            {"kind": "deadend", "direction": "直接写 php", "reason": "被 waf 拦"},
        ],
    )
    (run / "winner.json").write_text(json.dumps({"flag": "flag{abc_123}", "reason": "三重校验通过"}), encoding="utf-8")

    out = generate_writeup(run)
    assert out is not None and out.name == "writeup.md"
    text = out.read_text(encoding="utf-8")
    assert "一道文件写入题" in text  # 目标
    assert "http://t:8080" in text
    assert "flag{abc_123}" in text  # flag
    assert "8080 是 nginx" in text  # 已验证事实
    assert "直接写 php" in text and "被 waf 拦" in text  # 已排除方向
    assert "http_request" in text and "shell_exec" in text  # 时间线
    assert "## 时间线" in text


def test_writeup_without_flag(tmp_path):
    run = tmp_path / "run2"
    run.mkdir()
    _write_jsonl(
        run / "rollout.jsonl",
        [
            {"type": "session_meta", "run_id": "run2", "objective": "失败的题", "ts": time.time()},
            {"type": "tool_call", "ts": time.time(), "calls": [{"id": "c1", "name": "shell_exec", "arguments": '{"command":"id"}'}]},
        ],
    )
    out = generate_writeup(run)
    assert out is not None
    text = out.read_text(encoding="utf-8")
    assert "未拿到 flag" in text
    assert "（无）" in text  # 空事实/死路区块


def test_writeup_no_rollout(tmp_path):
    assert generate_writeup(tmp_path / "ghost") is None
