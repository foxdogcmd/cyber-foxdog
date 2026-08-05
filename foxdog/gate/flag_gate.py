"""FlagGate——反幻觉闸门（整体照搬 muteki solver/gate.py 语义）。

submit_flag 必须通过三重校验才被接受：
1. 格式：匹配 run 配置的 flag_format 正则；未配置时要求长度≥8 且含 {} 结构。
2. 非占位符：拒绝小写占位、纯 x/N 重复串、uuid、含空格/逗号集合、
   嵌套花括号（f-string 表达式）、example/test/dummy/placeholder 等关键词。
3. 溯源：flag 必须逐字出现在该 run 已落盘的真实工具输出归档（CAS 全部文件）
   或给定 evidence 句柄的内容里；只在模型散文里出现的不算。

通过后写 winner.json 并置 solved（first-valid-flag-wins）。
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from ..store.workspace import Workspace


class FlagGateResult(BaseModel):
    ok: bool
    reason: str
    flag: str


# 占位 body 精确匹配清单（每条都对应一次真实翻车，持续增补）
_PLACEHOLDER_BODIES = {
    "...",
    "abc",
    "flag",
    "here",
    "your_flag",
    "your_flag_here",
    "insert_flag_here",
    "example",
    "test",
    "dummy",
    "placeholder",
    "sample",
    "fake",
    "todo",
    "none",
    "null",
    "xxx",
}

# 子串级高危关键词（body 包含即拒）
_PLACEHOLDER_SUBSTR = ("your_flag", "placeholder", "example", "dummy", "insert_flag")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REPEAT_RE = re.compile(r"^([xX]+|[nN]+)$")  # 纯 x/N 重复串

# 教科书/RFC 里的示例 UUID（模型幻觉高发）——但 GZCTF/PCTF 等动态 flag 平台的
# 真 flag body 就是随机 UUID，不能一票否决（run 20260802-144323 误杀真 flag）
_KNOWN_EXAMPLE_UUIDS = {
    "550e8400-e29b-41d4-a716-446655440000",  # RFC 4122 示例
    "123e4567-e89b-12d3-a456-426614174000",  # 维基百科示例
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
}


def _example_uuid(body: str) -> bool:
    """只拒教科书示例 UUID；随机 UUID 是合法动态 flag，交给溯源+时序校验把关。"""
    low = body.lower()
    if not _UUID_RE.fullmatch(low):
        return False
    if low in _KNOWN_EXAMPLE_UUIDS:
        return True
    # 全段同一字符（如 11111111-1111-...）视为占位
    return len(set(low.replace("-", ""))) <= 2


def _extract_body(flag: str) -> str:
    m = re.search(r"\{(.*)\}", flag)
    return m.group(1) if m else flag


def placeholder_reason(flag: str) -> str | None:
    """是占位符则返回原因，否则 None。"""
    if " " in flag or "," in flag:
        return "含空格/逗号，疑似集合体或散文"
    body = _extract_body(flag).strip()
    low = body.lower()
    if low in _PLACEHOLDER_BODIES:
        return f"占位符 body: {body!r}"
    if any(k in low for k in _PLACEHOLDER_SUBSTR):
        return f"含占位关键词: {body!r}"
    if _REPEAT_RE.fullmatch(body):
        return f"纯 x/N 重复串: {body!r}"
    if _example_uuid(body):
        return f"示例 uuid 占位: {body!r}"
    if "{" in body or "}" in body:
        return "嵌套花括号，疑似 f-string 表达式"
    return None


class FlagGate:
    def __init__(self, workspace: Workspace, flag_format: str | None = None, expected_flags: int = 1) -> None:
        self.workspace = workspace
        # 多 flag 支持（一题可能有多面 flag）：expected_flags 默认 1，语义与旧版完全一致
        self.expected_flags = max(1, expected_flags)
        self.flags: list[str] = []
        self.flag: str | None = None  # 最后一面过闸的 flag（展示/提交平台用）
        self._format_re = re.compile(flag_format) if flag_format else None
        # 自我注入防护（时序规则）：agent 的工具输入与工具输出分别记录 flag 样
        # 字符串的首次出现序号。输入先于输出 → 自我注入（echo 自编 flag 再提交，
        # run 20260802-125736 实战翻车），拒收；输出先于输入 → 合法
        # （解密/计算出的 flag 先出现在输出，之后打進验证命令也放行）。
        self._first_seen_input: dict[str, int] = {}
        self._first_seen_output: dict[str, int] = {}
        self._clock = 0
        # 提取用正则：格式配置去掉 fullmatch 语义的锚点，供 findall 搜索
        src = flag_format.strip("^$") if flag_format else r"\w+\{[^{}]*\}"
        self._extract_re = re.compile(src)

    @property
    def _tainted(self) -> set[str]:
        """兼容旧接口：输入先于输出（或仅出现在输入）的 flag 集合。"""
        return {
            f
            for f, ti in self._first_seen_input.items()
            if ti < self._first_seen_output.get(f, 1 << 62)
        }

    def _extract(self, text: str) -> list[str]:
        out: list[str] = []
        for m in self._extract_re.findall(text):
            if isinstance(m, tuple):  # 格式正则含分组时 findall 返回元组
                m = next((g for g in m if g), "")
            if m:
                out.append(m)
        return out

    def taint_scan(self, text: str | None) -> None:
        """扫描 agent 的工具输入（命令/写文件内容/代码），记录 flag 样字符串首现序号。

        注意：不扫 submit_flag 自身参数（否则合法提交自锁），也不扫助手散文
        （散文过不了溯源；引用真 flag 是合法行为）。"""
        if not text:
            return
        self._clock += 1
        for m in self._extract(text):
            self._first_seen_input.setdefault(m, self._clock)

    def output_scan(self, text: str | None) -> None:
        """扫描工具输出，记录 flag 样字符串首现序号（时序规则的另一半）。"""
        if not text:
            return
        self._clock += 1
        for m in self._extract(text):
            self._first_seen_output.setdefault(m, self._clock)

    def _format_ok(self, flag: str) -> bool:
        if self._format_re is not None:
            return self._format_re.fullmatch(flag) is not None
        return len(flag) >= 8 and "{" in flag and "}" in flag

    def _traced(self, flag: str, evidence: str | None) -> bool:
        """溯源：flag 逐字出现在 CAS 归档或 evidence 句柄内容里。"""
        if evidence and self.workspace.cas_exists(evidence):
            if flag in self.workspace.cas_fetch_text(evidence):
                return True
        needle = flag.encode("utf-8")
        for path in self.workspace.cas_files():
            try:
                if needle in path.read_bytes():
                    return True
            except OSError:
                continue
        return False

    @property
    def solved(self) -> bool:
        """凑齐 expected_flags 面才算 solved（默认 1 面，语义同旧版 first-valid-wins）。"""
        return len(self.flags) >= self.expected_flags

    def submit(self, flag: str, evidence: str | None = None) -> FlagGateResult:
        flag = flag.strip()
        if flag in self.flags:
            return FlagGateResult(
                ok=True,
                reason=f"该 flag 已计数（{len(self.flags)}/{self.expected_flags}），忽略重复提交。",
                flag=flag,
            )
        if self.solved:
            return FlagGateResult(ok=True, reason=f"本题已集齐（{self.flags}），忽略新提交。", flag=flag)
        if not flag:
            return FlagGateResult(ok=False, reason="空 flag", flag=flag)
        # 1. 格式
        if not self._format_ok(flag):
            return FlagGateResult(ok=False, reason=f"格式校验失败：不匹配 flag_format 或哨兵规则: {flag!r}", flag=flag)
        # 2. 非占位符
        reason = placeholder_reason(flag)
        if reason:
            return FlagGateResult(ok=False, reason=f"占位符校验失败：{reason}", flag=flag)
        # 2.5 非自我注入（时序）：flag 先出现在 agent 工具输入、后才出现在输出 → 拒收
        if flag in self._tainted:
            return FlagGateResult(
                ok=False,
                reason="溯源校验失败：候选 flag 先出现在 agent 自己的工具输入（命令/写入内容）中，其后才出现在输出里，疑似自我注入；目标产出或解密计算（输出在先）的 flag 不受此限",
                flag=flag,
            )
        # 3. 溯源
        if not self._traced(flag, evidence):
            return FlagGateResult(
                ok=False,
                reason="溯源校验失败：flag 未逐字出现在真实工具输出归档（CAS）中；模型散文不算数",
                flag=flag,
            )
        # 通过：累积计数，凑齐 expected_flags 面才 solved
        self.flags.append(flag)
        self.flag = flag
        self.workspace.write_winner(flag, reason="FlagGate 四重校验通过", flags=self.flags, solved=self.solved)
        return FlagGateResult(
            ok=True,
            reason=f"flag 验证通过（第 {len(self.flags)}/{self.expected_flags} 面）: {flag}",
            flag=flag,
        )
