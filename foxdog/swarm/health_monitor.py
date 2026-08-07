"""外部依赖健康监控。

目标不是统计所有工具失败，而是从工具事件中识别“真实靶机/外部服务”失败：
HTTP 请求、远程 shell 命令、后台远程探测等。planner/coordinator 读取这里的
状态后，可以尽早提示操作员并把暂停远程探测的 directive 写入黑板。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse


@dataclass
class HealthEvent:
    """一次外部依赖尝试。timestamp 使用 monotonic 秒，便于和 coordinator 对齐。"""

    timestamp: float
    success: bool
    error_type: str | None = None  # EOF | timeout | connection_refused | reset | unreachable | other
    latency_ms: float | None = None
    source: str = ""


HealthStatus = Literal["healthy", "degraded", "critical", "dead"]


@dataclass
class HealthAssessment:
    """健康评估结果。"""

    status: HealthStatus
    diagnosis: str
    should_escalate: bool
    recommendation: str = ""


_NETWORK_CMD_RE = re.compile(
    r"\b(?:nc|ncat|netcat|socat|curl|wget|telnet|ssh|ftp)\b|"
    r"\bopenssl\s+s_client\b|https?://|\b(?:pwntools|remote\(|socket\.|connect\()\b",
    re.IGNORECASE,
)
_EXPLOIT_CMD_RE = re.compile(r"\b(?:exploit|exp(?:loit)?\.py|pwn|pwntools|remote)\b", re.IGNORECASE)
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("timeout", re.compile(r"\b(?:timeout|timed?\s*out|time\s*out|idle-timeout|i/o timeout)\b", re.IGNORECASE)),
    ("connection_refused", re.compile(r"connection refused|actively refused|econnrefused|winerror 10061", re.IGNORECASE)),
    ("reset", re.compile(r"connection reset|econnreset|forcibly closed|broken pipe|epipe", re.IGNORECASE)),
    ("unreachable", re.compile(r"network is unreachable|no route to host|host unreachable|name or service not known|temporary failure in name resolution", re.IGNORECASE)),
    ("EOF", re.compile(r"\bEOF\b|end of file|unexpected EOF|closed by foreign host|remote end closed", re.IGNORECASE)),
)


class ServiceHealthMonitor:
    """远程服务健康状态追踪与诊断。

    判定层级：
    - healthy: 当前失败链为空
    - degraded: 5 分钟内有失败，但未连续达到 critical
    - critical: 5 分钟内连续 N 次失败
    - dead: 15 分钟内失败达到阈值，或曾经成功后长时间失败且无恢复
    """

    def __init__(
        self,
        *,
        target: str | None = None,
        critical_threshold: int = 3,
        dead_threshold: int = 5,
        critical_window: float = 300.0,
        dead_window: float = 900.0,
        dead_timeout: float = 1800.0,
    ) -> None:
        self.target = target
        self.history: list[HealthEvent] = []
        self.last_success: float | None = None
        self.first_failure: float | None = None

        self.critical_threshold = critical_threshold
        self.dead_threshold = dead_threshold
        self.critical_window = critical_window
        self.dead_window = dead_window
        self.dead_timeout = dead_timeout

        self._last_assessment: HealthAssessment | None = None
        self._last_assessment_time: float = 0.0
        self._pending_calls: dict[tuple[str | None, str], tuple[str, str]] = {}

    # ------------------------------------------------------------ 事件观察
    def observe_event(self, event: Any) -> bool:
        """观察协议事件；返回是否记录了健康尝试。

        只处理 tool_call_begin/tool_call_end/background_task_complete。任何解析异常
        都吞掉，监控不能影响解题主流程。
        """
        try:
            kind = getattr(event, "kind", "")
            if kind == "tool_call_begin":
                self._pending_calls[(getattr(event, "worker", None), str(event.call_id))] = (
                    str(event.name),
                    str(getattr(event, "arguments", "") or ""),
                )
                return False
            if kind == "tool_call_end":
                key = (getattr(event, "worker", None), str(event.call_id))
                name, raw_args = self._pending_calls.pop(
                    key, (str(getattr(event, "name", "")), "")
                )
                return self.observe_tool_result(
                    name,
                    _json_args(raw_args),
                    ok=bool(getattr(event, "ok", False)),
                    error=getattr(event, "error", None),
                    output=str(getattr(event, "output_preview", "") or ""),
                    duration_ms=getattr(event, "duration_ms", None),
                )
            if kind == "background_task_complete":
                return self.observe_shell_command(
                    str(getattr(event, "command", "") or ""),
                    ok=bool(getattr(event, "ok", False)),
                    error=None,
                    output=str(getattr(event, "output_preview", "") or ""),
                    source="background_task",
                )
        except Exception:
            return False
        return False

    def observe_tool_result(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        ok: bool,
        error: str | None = None,
        output: str = "",
        duration_ms: float | None = None,
    ) -> bool:
        """从一次工具结果里提取外部依赖健康信号。"""
        if tool_name == "http_request":
            url = str(args.get("url", "") or "")
            if self.target and not self._mentions_target(url):
                return False
            return self._record_observed(ok, error, output, duration_ms, source=f"http_request:{url[:80]}")

        if tool_name == "shell_exec":
            return self.observe_shell_command(
                str(args.get("command", "") or ""),
                ok=ok,
                error=error,
                output=output,
                duration_ms=duration_ms,
                source="shell_exec",
            )

        if tool_name == "shell_session":
            text = f"{args.get('data', '')}\n{error or ''}\n{output or ''}"
            error_type = self._classify_error(text)
            if not error_type:
                return False
            return self._record_observed(ok=False, error=error_type, output=text, duration_ms=duration_ms, source="shell_session")

        return False

    def observe_shell_command(
        self,
        command: str,
        *,
        ok: bool,
        error: str | None = None,
        output: str = "",
        duration_ms: float | None = None,
        source: str = "shell_exec",
    ) -> bool:
        """观察 shell 命令或后台任务。非远程命令会被跳过。"""
        if not self._is_remote_shell_observation(command, error or "", output or "", ok):
            return False
        return self._record_observed(ok, error, output, duration_ms, source=f"{source}:{command[:100]}")

    # ------------------------------------------------------------ 手工记录/评估
    def record_attempt(
        self,
        success: bool,
        error_type: str | None = None,
        latency_ms: float | None = None,
        source: str = "",
    ) -> None:
        """记录一次外部依赖尝试。"""
        now = time.monotonic()
        self.history.append(
            HealthEvent(
                timestamp=now,
                success=success,
                error_type=error_type,
                latency_ms=latency_ms,
                source=source,
            )
        )

        if success:
            self.last_success = now
        self._prune_history(now)
        self._last_assessment = None

    def assess_health(self, force: bool = False) -> HealthAssessment:
        now = time.monotonic()
        if not force and self._last_assessment and now - self._last_assessment_time < 5.0:
            return self._last_assessment

        self._prune_history(now)
        active_failures_5min = self._active_failures(now, self.critical_window)
        active_failures_15min = self._active_failures(now, self.dead_window)

        if not active_failures_5min and not active_failures_15min:
            return self._cache_assessment(HealthAssessment("healthy", "", False))

        dead_result = self._check_dead(now, active_failures_15min)
        if dead_result:
            return self._cache_assessment(dead_result)

        critical_result = self._check_critical(active_failures_5min)
        if critical_result:
            return self._cache_assessment(critical_result)

        return self._cache_assessment(
            HealthAssessment(
                status="degraded",
                diagnosis=f"最近 {len(active_failures_5min)} 次失败",
                should_escalate=False,
                recommendation="继续监控，暂不干预",
            )
        )

    def reset(self) -> None:
        self.history.clear()
        self.last_success = None
        self.first_failure = None
        self._last_assessment = None
        self._pending_calls.clear()

    # ------------------------------------------------------------ 内部判定
    def _record_observed(
        self,
        ok: bool,
        error: str | None,
        output: str,
        duration_ms: float | None,
        *,
        source: str,
    ) -> bool:
        error_type = None if ok else (self._classify_error(f"{error or ''}\n{output or ''}") or "other")
        self.record_attempt(ok, error_type=error_type, latency_ms=duration_ms, source=source)
        return True

    def _active_failures(self, now: float, window: float) -> list[HealthEvent]:
        after_success = self.last_success if self.last_success is not None else float("-inf")
        return [
            e
            for e in self.history
            if not e.success and e.timestamp > after_success and now - e.timestamp < window
        ]

    def _check_dead(self, now: float, failures_15min: list[HealthEvent]) -> HealthAssessment | None:
        if len(failures_15min) >= self.dead_threshold:
            counts: dict[str, int] = {}
            for failure in failures_15min:
                key = failure.error_type or "unknown"
                counts[key] = counts.get(key, 0) + 1
            dominant_error = max(counts.items(), key=lambda item: item[1])
            return HealthAssessment(
                status="dead",
                diagnosis=f"15分钟内 {len(failures_15min)} 次失败 (主要: {dominant_error[0]} x{dominant_error[1]})",
                should_escalate=True,
                recommendation=(
                    "服务可能已崩溃，建议:\n"
                    "1. 联系 CTF 管理员报告服务故障\n"
                    "2. 检查是否有备用靶机\n"
                    "3. 暂停相关 intent，切换到本地验证"
                ),
            )

        if (
            self.last_success is not None
            and self.first_failure is not None
            and self.first_failure > self.last_success
            and now - self.last_success > self.dead_timeout
        ):
            return HealthAssessment(
                status="dead",
                diagnosis=f"{(now - self.last_success) / 60:.0f} 分钟无成功响应",
                should_escalate=True,
                recommendation="长时间无响应，建议暂停所有远程操作",
            )
        return None

    def _check_critical(self, failures_5min: list[HealthEvent]) -> HealthAssessment | None:
        if len(failures_5min) < self.critical_threshold:
            return None
        recent = sorted(self.history[-20:], key=lambda event: event.timestamp)
        after_success = self.last_success if self.last_success is not None else float("-inf")
        consecutive_failures: list[HealthEvent] = []
        for event in reversed(recent):
            if event.timestamp <= after_success:
                break
            if event.success:
                break
            consecutive_failures.append(event)
        if len(consecutive_failures) < self.critical_threshold:
            return None
        errors = [event.error_type or "unknown" for event in reversed(consecutive_failures[: self.critical_threshold])]
        return HealthAssessment(
            status="critical",
            diagnosis=f"连续 {len(consecutive_failures)} 次失败 ({', '.join(errors)})",
            should_escalate=True,
            recommendation=(
                "服务出现严重问题，建议:\n"
                "1. 检查网络连接\n"
                "2. 确认靶机 IP/端口\n"
                "3. 查看 CTF 平台公告"
            ),
        )

    def _prune_history(self, now: float) -> None:
        cutoff = now - max(self.dead_timeout, self.dead_window, self.critical_window)
        self.history = [event for event in self.history if event.timestamp > cutoff]
        after_success = self.last_success if self.last_success is not None else float("-inf")
        active_failures = [event for event in self.history if not event.success and event.timestamp > after_success]
        self.first_failure = active_failures[0].timestamp if active_failures else None

    def _cache_assessment(self, assessment: HealthAssessment) -> HealthAssessment:
        self._last_assessment = assessment
        self._last_assessment_time = time.monotonic()
        return assessment

    def _is_remote_shell_observation(self, command: str, error: str, output: str, ok: bool) -> bool:
        text = f"{command}\n{error}\n{output}"
        if self.target and self._mentions_target(text):
            return True
        if ok:
            return self.target is None and bool(_NETWORK_CMD_RE.search(command))
        if self._classify_error(text):
            return bool(_NETWORK_CMD_RE.search(command) or _EXPLOIT_CMD_RE.search(command) or not self.target)
        return self.target is None and bool(_NETWORK_CMD_RE.search(command))

    def _mentions_target(self, text: str) -> bool:
        target = (self.target or "").strip()
        if not target:
            return False
        tokens = _target_tokens(target)
        lowered = text.lower()
        return any(token and token.lower() in lowered for token in tokens)

    def _classify_error(self, text: str) -> str | None:
        for label, pattern in _ERROR_PATTERNS:
            if pattern.search(text):
                return label
        return None

    @property
    def total_attempts(self) -> int:
        return len(self.history)

    @property
    def success_count(self) -> int:
        return sum(1 for event in self.history if event.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for event in self.history if not event.success)

    @property
    def success_rate(self) -> float:
        if not self.history:
            return 1.0
        return self.success_count / len(self.history)


def _json_args(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _target_tokens(target: str) -> set[str]:
    raw = target.strip()
    if not raw:
        return set()
    tokens = {raw}
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    if parsed.netloc:
        tokens.add(parsed.netloc)
    if parsed.hostname:
        tokens.add(parsed.hostname)
        if parsed.port:
            tokens.add(f"{parsed.hostname}:{parsed.port}")
    return tokens
