from __future__ import annotations

from types import SimpleNamespace

from foxdog.core.budget import Budget
from foxdog.protocol.types import BackgroundTaskComplete, ToolCallBegin, ToolCallEnd
from foxdog.swarm.blackboard import Blackboard
from foxdog.swarm.coordinator import Coordinator
from foxdog.swarm.health_monitor import ServiceHealthMonitor
from foxdog.swarm.spectator import SpectatorReview


def test_health_monitor_initial_and_success_are_healthy():
    monitor = ServiceHealthMonitor()

    assert monitor.assess_health().status == "healthy"
    monitor.record_attempt(success=True, latency_ms=50)

    assessment = monitor.assess_health()
    assert assessment.status == "healthy"
    assert not assessment.should_escalate


def test_health_monitor_degraded_before_critical_threshold():
    monitor = ServiceHealthMonitor(critical_threshold=3)

    monitor.record_attempt(success=False, error_type="timeout")
    monitor.record_attempt(success=False, error_type="EOF")

    assessment = monitor.assess_health()
    assert assessment.status == "degraded"
    assert not assessment.should_escalate


def test_health_monitor_critical_after_consecutive_failures():
    monitor = ServiceHealthMonitor(critical_threshold=3)

    for _ in range(3):
        monitor.record_attempt(success=False, error_type="EOF")

    assessment = monitor.assess_health()
    assert assessment.status == "critical"
    assert assessment.should_escalate
    assert "连续" in assessment.diagnosis


def test_health_monitor_dead_after_dead_window_failure_count():
    monitor = ServiceHealthMonitor(dead_threshold=5, dead_window=60)

    for _ in range(5):
        monitor.record_attempt(success=False, error_type="EOF")

    assessment = monitor.assess_health()
    assert assessment.status == "dead"
    assert assessment.should_escalate
    assert "暂停" in assessment.recommendation


def test_health_monitor_success_recovery_cuts_failure_chain():
    monitor = ServiceHealthMonitor(critical_threshold=3)

    for _ in range(3):
        monitor.record_attempt(success=False, error_type="EOF")
    assert monitor.assess_health().status == "critical"

    monitor.record_attempt(success=True)

    assessment = monitor.assess_health(force=True)
    assert assessment.status == "healthy"
    assert monitor.failure_count == 3
    assert monitor.success_count == 1


def test_health_monitor_success_rate_keeps_history_counts():
    monitor = ServiceHealthMonitor()

    monitor.record_attempt(success=True)
    monitor.record_attempt(success=True)
    monitor.record_attempt(success=False, error_type="timeout")
    monitor.record_attempt(success=True)
    monitor.record_attempt(success=False, error_type="EOF")

    assert monitor.total_attempts == 5
    assert monitor.success_count == 3
    assert monitor.failure_count == 2
    assert abs(monitor.success_rate - 0.6) < 0.01


def test_tool_events_record_remote_shell_failure():
    monitor = ServiceHealthMonitor(target="challenge.example:31337")

    assert not monitor.observe_event(
        ToolCallBegin(
            call_id="c1",
            name="shell_exec",
            arguments='{"command":"python exp.py challenge.example 31337"}',
            worker="worker-1",
        )
    )
    assert monitor.observe_event(
        ToolCallEnd(
            call_id="c1",
            name="shell_exec",
            ok=False,
            output_preview="EOF while reading remote banner",
            duration_ms=42,
            worker="worker-1",
        )
    )

    assert monitor.failure_count == 1
    assert monitor.history[-1].error_type == "EOF"
    assert monitor.history[-1].source.startswith("shell_exec:")


def test_local_shell_failure_is_ignored_when_target_is_known():
    monitor = ServiceHealthMonitor(target="challenge.example:31337")

    recorded = monitor.observe_tool_result(
        "shell_exec",
        {"command": "python local_parser.py sample.bin"},
        ok=False,
        error="ValueError: unexpected EOF in local file",
    )

    assert not recorded
    assert monitor.total_attempts == 0


def test_background_remote_failure_is_recorded():
    monitor = ServiceHealthMonitor(target="challenge.example:31337")

    assert monitor.observe_event(
        BackgroundTaskComplete(
            task_id="bg-1",
            command="curl -sS http://challenge.example:31337/",
            ok=False,
            exit_code=7,
            output_preview="curl: (7) Connection refused",
        )
    )

    assert monitor.failure_count == 1
    assert monitor.history[-1].error_type == "connection_refused"


async def test_coordinator_health_warning_posts_directive(tmp_path):
    board = Blackboard(tmp_path / "health.db")
    monitor = ServiceHealthMonitor(target="127.0.0.1:1", critical_threshold=3)
    for _ in range(3):
        monitor.record_attempt(success=False, error_type="EOF", source="shell_exec:nc 127.0.0.1 1")
    events = []
    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="pwn target",
        budget=Budget(),
        target="127.0.0.1:1",
        health_monitor=monitor,
    )
    coord._last_health_check = -999.0

    try:
        await coord._check_service_health()

        notes = [getattr(event, "message", "") for event in events]
        directives = board.directives()
        assert any("远程服务告警" in note for note in notes)
        assert len(directives) == 1
        assert directives[0]["actor"] == "health_monitor"
        assert "建议暂停所有远程连接操作" in directives[0]["content"]
    finally:
        board.close()


async def test_spectator_external_block_posts_deduped_directive(tmp_path):
    board = Blackboard(tmp_path / "external-block.db")
    board.post_fact("worker 报告远程 EOF", actor="worker-1")

    class BlockedSpectator:
        async def review(self, *_args, **_kwargs):
            return SpectatorReview(
                verdict="continue",
                summary="远程连接多次 EOF",
                blocked_by_external="remote:challenge.example:31337",
            )

    events = []
    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="pwn target",
        budget=Budget(),
        spectator=BlockedSpectator(),
        spectator_cooldown=0.0,
    )

    try:
        assert await coord._spectator_once() is None
        assert await coord._spectator_once() is None

        directives = board.directives()
        notes = [getattr(event, "message", "") for event in events]
        assert len(directives) == 1
        assert directives[0]["actor"] == "spectator"
        assert directives[0]["priority"] == "high"
        assert "remote:challenge.example:31337" in directives[0]["content"]
        assert "本地复现" in directives[0]["content"]
        assert any("外部依赖阻塞" in note for note in notes)
    finally:
        board.close()
