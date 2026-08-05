"""FlagGate 三重校验：格式 / 占位符 / 溯源，正反用例。"""
from __future__ import annotations

import pytest

from foxdog.gate.flag_gate import FlagGate, placeholder_reason
from foxdog.store.workspace import Workspace


@pytest.fixture()
def ws(tmp_path):
    return Workspace(base=tmp_path, run_id="test-run")


@pytest.fixture()
def gate(ws):
    return FlagGate(ws, flag_format=r"flag\{.*?\}")


def _archive(ws: Workspace, text: str) -> str:
    return ws.cas_store(text.encode())


# ------------------------------------------------------------ 格式
class TestFormat:
    def test_ok(self, ws, gate):
        _archive(ws, "leaked: flag{real_abc123}")
        r = gate.submit("flag{real_abc123}")
        assert r.ok, r.reason

    def test_format_mismatch(self, gate):
        r = gate.submit("nope{abc12345}")
        assert not r.ok
        assert "格式" in r.reason

    def test_no_format_sentinel_ok(self, ws):
        g = FlagGate(ws)  # 未配置 flag_format → 哨兵规则
        _archive(ws, "FLAG{abcdefgh} found")
        assert g.submit("FLAG{abcdefgh}").ok

    def test_no_format_sentinel_reject_short(self, ws):
        g = FlagGate(ws)
        _archive(ws, "ab{cd}")
        assert not g.submit("ab{cd}").ok  # 长度 < 8

    def test_no_format_sentinel_reject_no_braces(self, ws):
        g = FlagGate(ws)
        _archive(ws, "abcdefghijklmn")
        assert not g.submit("abcdefghijklmn").ok


# ------------------------------------------------------------ 占位符（逐条）
PLACEHOLDERS = [
    "flag{...}",
    "flag{abc}",
    "flag{your_flag_here}",
    "flag{your_flag}",
    "flag{insert_flag_here}",
    "flag{example}",
    "flag{test}",
    "flag{dummy}",
    "flag{placeholder}",
    "flag{sample}",
    "flag{fake}",
    "flag{xxxx}",
    "flag{NNNN}",
    "flag{550e8400-e29b-41d4-a716-446655440000}",  # uuid
    "flag{has space inside}",
    "flag{a,b,c}",  # 逗号集合
    "flag{f'{x}'}",  # f-string / 嵌套花括号
    "flag{EXAMPLE_x9}",  # 关键词子串，大小写不敏感
]


class TestPlaceholder:
    @pytest.mark.parametrize("flag", PLACEHOLDERS)
    def test_rejected_by_rule(self, flag):
        assert placeholder_reason(flag) is not None, flag

    @pytest.mark.parametrize("flag", PLACEHOLDERS)
    def test_rejected_by_gate(self, ws, gate, flag):
        _archive(ws, f"output contains {flag}")  # 即使溯源能过也必须被占位符拦截
        r = gate.submit(flag)
        assert not r.ok
        assert "占位符" in r.reason

    def test_real_flag_not_placeholder(self):
        assert placeholder_reason("flag{test_123}") is None
        assert placeholder_reason("flag{r3al_fl4g_9f2e}") is None


# ------------------------------------------------------------ 溯源
class TestProvenance:
    def test_flag_in_archived_output(self, ws, gate):
        _archive(ws, "HTTP 200\n\nwelcome, flag{traced_9f2e7}")
        r = gate.submit("flag{traced_9f2e7}")
        assert r.ok

    def test_flag_only_in_prose_rejected(self, gate):
        r = gate.submit("flag{prose_only_123}")
        assert not r.ok
        assert "溯源" in r.reason

    def test_evidence_handle(self, ws, gate):
        handle = _archive(ws, "root shell: flag{via_handle_777}")
        r = gate.submit("flag{via_handle_777}", evidence=handle)
        assert r.ok

    def test_evidence_bad_handle_still_scans_cas(self, ws, gate):
        _archive(ws, "flag{cas_fallback_5}")
        r = gate.submit("flag{cas_fallback_5}", evidence="0" * 64)
        assert r.ok

    def test_verbatim_required(self, ws, gate):
        _archive(ws, "flag{case_SENSITIVE_1}")
        assert not gate.submit("flag{case_sensitive_1}").ok  # 大小写不同不算逐字
        assert gate.submit("flag{case_SENSITIVE_1}").ok

    def test_first_valid_wins_and_winner_json(self, ws, gate):
        _archive(ws, "flag{winner_abc} and flag{loser_def}")
        assert gate.submit("flag{winner_abc}").ok
        assert gate.solved and gate.flag == "flag{winner_abc}"
        r2 = gate.submit("flag{loser_def}")
        assert r2.ok and "已集齐" in r2.reason
        winner = ws.read_winner()
        assert winner and winner["flag"] == "flag{winner_abc}"


# ------------------------------------------------------------ 自我注入污染（run 20260802-125736 实战翻车）
class TestTaint:
    def test_agent_injected_flag_rejected(self, ws, gate):
        # agent 通过 RCE/写文件把自编 flag 放进工具输入 → 即使它随后出现在输出归档里也拒收
        gate.taint_scan('{"command": "echo \\"PROBE flag{probe123456}\\""}')
        _archive(ws, "PROBE flag{probe123456}")
        r = gate.submit("flag{probe123456}")
        assert not r.ok
        assert "自我注入" in r.reason or "工具输入" in r.reason

    def test_target_produced_flag_not_tainted(self, ws, gate):
        # 目标自己产出的 flag：工具输入里从未出现过 → 正常放行
        gate.taint_scan('{"command": "cat /flag"}')
        _archive(ws, "flag{real_from_target_9z}")
        assert gate.submit("flag{real_from_target_9z}").ok

    def test_taint_scan_extracts_multiple(self, gate):
        gate.taint_scan('a flag{one_111111} b flag{two_222222} c')
        assert "flag{one_111111}" in gate._tainted
        assert "flag{two_222222}" in gate._tainted

    def test_taint_scan_none_and_empty(self, gate):
        gate.taint_scan(None)
        gate.taint_scan("")
        assert not gate._tainted

    def test_default_extract_without_format(self, ws):
        g = FlagGate(ws)  # 无 flag_format → 通用 \w+\{...\} 提取
        g.taint_scan("echo FLAG{selfmade_99}")
        _archive(ws, "FLAG{selfmade_99}")
        assert not g.submit("FLAG{selfmade_99}").ok


class TestTaintOrdering:
    def test_output_first_then_input_allowed(self, ws, gate):
        # 解密/计算型：flag 先出现在输出（python_eval 打印），后出现在输入（验证命令）→ 合法
        gate.output_scan("decrypted: flag{decrypted_abc999}")
        gate.taint_scan('{"command": "curl -d flag=flag{decrypted_abc999} ..."}')
        _archive(ws, "decrypted: flag{decrypted_abc999}")
        r = gate.submit("flag{decrypted_abc999}")
        assert r.ok, r.reason

    def test_input_first_then_output_rejected(self, ws, gate):
        # 自我注入：先打进命令，后 echo 到输出 → 拒收
        gate.taint_scan('{"command": "echo flag{injected_111}"}')
        gate.output_scan("flag{injected_111}")
        _archive(ws, "flag{injected_111}")
        assert not gate.submit("flag{injected_111}").ok

    def test_write_then_cat_rejected(self, ws, gate):
        # 间接注入：write_file 写入（输入在先）→ cat 读出（输出在后）→ 仍拒收
        gate.taint_scan('{"path": "x.txt", "content": "flag{indirect_222}"}')
        gate.output_scan("flag{indirect_222}")
        _archive(ws, "flag{indirect_222}")
        assert not gate.submit("flag{indirect_222}").ok


class TestUuidPolicy:
    def test_rfc_example_uuid_rejected(self, gate):
        r = placeholder_reason("flag{550e8400-e29b-41d4-a716-446655440000}")
        assert r and "uuid" in r

    def test_wiki_example_uuid_rejected(self, gate):
        assert placeholder_reason("flag{123e4567-e89b-12d3-a456-426614174000}")

    def test_same_char_uuid_rejected(self, gate):
        assert placeholder_reason("flag{11111111-1111-1111-1111-111111111111}")

    def test_random_uuid_allowed(self, gate):
        # GZCTF/PCTF 动态 flag 的 body 就是随机 UUID（run 20260802-144323 误杀）
        assert placeholder_reason("flag{8f71b17b-3cdf-41cb-a329-2b34ce3a2fb9}") is None

    def test_random_uuid_submit_ok(self, ws, gate):
        _archive(ws, "$flag='flag{8f71b17b-3cdf-41cb-a329-2b34ce3a2fb9}';")
        r = gate.submit("flag{8f71b17b-3cdf-41cb-a329-2b34ce3a2fb9}")
        assert r.ok, r.reason
