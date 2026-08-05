"""ReasonPlanner：合法/围栏包裹/非法 JSON 三种解析，verdict fallback，重试一次。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from foxdog.core.goal import EXIT_BUDGET, EXIT_COMPLETE, EXIT_ERROR
from foxdog.llm.client import FakeProvider
from foxdog.swarm.planner import ReasonPlanner, parse_plan

VALID = '{"verdict": "explore", "intents": [{"goal": "读 /flag", "category": "file", "surface": "local", "technique": "read"}], "audit": [2]}'


def test_parse_valid():
    r = parse_plan(VALID)
    assert r.verdict == "explore"
    assert len(r.intents) == 1 and r.intents[0].goal == "读 /flag"
    assert r.intents[0].category == "file" and r.audit == [2]


def test_parse_fenced():
    r = parse_plan(f"好的，以下是计划：\n```json\n{VALID}\n```\n希望有帮助")
    assert len(r.intents) == 1 and r.intents[0].technique == "read"


def test_parse_invalid_verdict_fallback():
    r = parse_plan('{"verdict": "destroy", "intents": []}')
    assert r.verdict == "explore"


def test_parse_garbage_returns_empty_explore():
    r = parse_plan("这不是 JSON，我拒绝输出")
    assert r.verdict == "explore" and r.intents == []


def test_parse_intents_clamped_to_4():
    intents = [{"goal": f"g{i}", "category": "c", "surface": "s", "technique": "t"} for i in range(7)]
    import json

    r = parse_plan(json.dumps({"verdict": "explore", "intents": intents}))
    assert len(r.intents) == 4


def test_parse_dynamic_workers_and_complete_summary():
    r = parse_plan('{"verdict":"complete","desired_workers":3,"summary":"已完成审计","intents":[]}')
    assert r.desired_workers == 3
    assert r.summary == "已完成审计"


async def test_planner_complete_exits_once_without_retry_loop(tmp_path):
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.planner import PlanResult

    board = Blackboard(tmp_path / "complete.db")

    class CompletePlanner:
        calls = 0

        async def plan(self, *_args, **_kwargs):
            self.calls += 1
            return PlanResult(verdict="complete", summary="已取得账号，但 RCE 路径均有证据排除")

    planner = CompletePlanner()
    events = []
    coord = Coordinator(
        board=board,
        planner=planner,
        factory=SimpleNamespace(budget_state=None),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="审计目标",
        budget=Budget(),
        tick=0.005,
        planner_cooldown=0.01,
    )
    code = await asyncio.wait_for(coord.solve(), timeout=1)
    assert code == EXIT_COMPLETE
    assert planner.calls == 1
    assert any("RCE 路径" in getattr(event, "message", "") for event in events)
    board.close()


async def test_spectator_complete_drives_exit_and_final_summary(tmp_path):
    """旁观者判定 complete → coordinator 收口 EXIT_COMPLETE，final_summary 供 writeup。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.planner import PlanResult
    from foxdog.swarm.spectator import SpectatorReview

    board = Blackboard(tmp_path / "spectator.db")

    class QuietPlanner:
        async def plan(self, *_args, **_kwargs):
            return PlanResult(verdict="explore", intents=[])

    class CompleteSpectator:
        calls = 0

        async def review(self, *_args, **_kwargs):
            self.calls += 1
            return SpectatorReview(verdict="complete", summary="旁观者：账号已拿到，无 RCE 面，任务收口")

    spectator = CompleteSpectator()
    events = []
    coord = Coordinator(
        board=board,
        planner=QuietPlanner(),
        factory=SimpleNamespace(budget_state=None),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="通用安全审计",
        budget=Budget(),
        tick=0.005,
        planner_cooldown=0.01,
        spectator=spectator,
    )
    # 灌一条事实 bump board.version()，让 _spectator_once 触发
    board.post_fact("worker 报告：已取得 admin 凭据", actor="worker-1")
    code = await asyncio.wait_for(coord.solve(), timeout=2)
    assert code == EXIT_COMPLETE
    assert spectator.calls >= 1
    assert "任务收口" in coord.final_summary
    assert any("spectator 确认任务完成" in getattr(e, "message", "") for e in events)
    board.close()


async def test_spectator_correct_is_directive_not_fact(tmp_path):
    """旁观者纠偏是控制指令，不污染已验证事实。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.spectator import SpectatorReview

    board = Blackboard(tmp_path / "spectator-correct.db")
    board.post_fact("8080 是 nginx", actor="worker-1")

    class CorrectSpectator:
        async def review(self, *_args, **_kwargs):
            return SpectatorReview(verdict="correct", instruction="先验证 /admin 的 302 跳转")

    events = []
    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="审计目标",
        budget=Budget(),
        spectator=CorrectSpectator(),
    )
    assert await coord._spectator_once() is None
    assert [f["content"] for f in board.facts()] == ["8080 是 nginx"]
    directives = board.directives()
    assert len(directives) == 1
    assert directives[0]["actor"] == "spectator"
    assert "先验证 /admin" in directives[0]["content"]
    assert "控制指令" in board.summary() and "先验证 /admin" in board.summary()
    assert any("spectator 纠偏" in getattr(event, "message", "") for event in events)
    board.close()


async def test_cancelled_worker_not_reported_as_completed(tmp_path):
    """被抢占（cancel）的 worker 不再误报"完成（无汇报）"；未 solved 时记 barren+被抢占中断。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator

    board = Blackboard(tmp_path / "cancel.db")
    board.create_intent("侦察靶机", "recon", "remote", "scan")

    class BlockingLoop:
        async def run_turn(self, history, turn_no=1):
            await asyncio.sleep(3600)  # 永远跑，等外部 cancel

    events = []
    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None, extra_providers=[], make_loop=lambda wid, provider=None: BlockingLoop()),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False, expected_flags=1, flags=[]),
        objective="审计",
        budget=Budget(),
    )
    task = asyncio.create_task(coord._run_worker("worker-1"))
    await asyncio.sleep(0.2)  # 让它认领并进入 run_turn
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    notes = [getattr(e, "message", "") for e in events if getattr(e, "kind", "") == "swarm_note"]
    # 不得出现"完成…无汇报"；应是"被抢占中断"
    assert not any("完成" in n and "无汇报" in n for n in notes), notes
    assert any("被抢占中断" in n for n in notes), notes
    intents = {i["id"]: i for i in board.intents()}
    assert all(i["status"] == "barren" for i in intents.values()), intents
    board.close()


async def test_user_worker_limit_ignores_planner_desired_workers(tmp_path):
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.planner import PlanResult

    board = Blackboard(tmp_path / "workers.db")

    class DynamicPlanner:
        async def plan(self, *_args, **_kwargs):
            return PlanResult(verdict="explore", desired_workers=2)

    coord = Coordinator(
        board=board,
        planner=DynamicPlanner(),
        factory=None,
        emit=lambda _event: None,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="目标",
        budget=Budget(),
        workers=4,
    )
    await coord._plan_once()
    assert coord.workers == 4
    assert coord.desired_workers == 4
    assert coord.set_worker_limit(2) == 4
    assert coord.workers == 2 and coord.desired_workers == 2
    assert coord.set_worker_limit(6) == 2
    assert coord.workers == 6 and coord.desired_workers == 6
    board.close()


async def test_plan_retries_once_on_bad_json():
    provider = FakeProvider(script=[("text", "垃圾输出"), ("text", VALID)])
    planner = ReasonPlanner(provider)
    r = await planner.plan("题目", "黑板摘要")
    assert provider.calls == 2 and len(r.intents) == 1


async def test_plan_gives_up_after_retry():
    provider = FakeProvider(script=[("text", "垃圾1"), ("text", "垃圾2")])
    planner = ReasonPlanner(provider)
    r = await planner.plan("题目", "黑板摘要")
    assert provider.calls == 2 and r.verdict == "explore" and r.intents == []


async def test_coordinator_rejects_placeholder_target(tmp_path):
    """planner 派的 intent 含占位主机名（http://target/...）必须被 coordinator 拒收。

    实战翻车：planner 上下文没有真实 URL，派了 http://target:80/ 的 intent，
    worker 对着不存在的占位主机打了 15 分钟。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.planner import PlanResult

    board = Blackboard(tmp_path / "bb.db")
    plan = PlanResult.model_validate(
        {
            "verdict": "explore",
            "intents": [
                {"goal": "向 http://target:80/ 发 multipart POST", "category": "web", "surface": "/", "technique": "rce"},
                {"goal": "读 http://challenge.imxbt.cn:30625/ 的源码", "category": "web", "surface": "/", "technique": "recon"},
            ],
        }
    )

    class _StubPlanner:
        async def plan(self, objective, summary, **_callbacks):
            assert "challenge.imxbt.cn:30625" in objective  # 规划上下文必须含真实地址
            return plan

    coord = Coordinator(
        board=board,
        planner=_StubPlanner(),
        factory=None,  # _plan_once 不触达 factory
        emit=lambda e: None,
        rollout=None,
        gate=None,
        objective="测试题",
        budget=Budget(max_minutes=1),
        target="http://challenge.imxbt.cn:30625/",
    )
    await coord._plan_once()
    goals = [i["goal"] for i in board.intents()]
    assert len(goals) == 1
    assert "challenge.imxbt.cn:30625" in goals[0]
    board.close()


async def test_planner_stream_is_visible_and_empty_plan_retries(tmp_path):
    """A slow/empty planner must be visible and must not leave the swarm idle forever."""
    from foxdog.core.budget import Budget
    from foxdog.protocol.types import AgentReasoningDelta, ModelRequestComplete, ModelRequestStarted
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.planner import PlanResult

    board = Blackboard(tmp_path / "planner-retry.db")
    interrupt = asyncio.Event()
    events = []

    class EmptyPlanner:
        calls = 0

        async def plan(self, _objective, _summary, *, on_delta=None, on_reasoning_delta=None):
            self.calls += 1
            if on_reasoning_delta is not None:
                on_reasoning_delta(f"规划第 {self.calls} 轮")
            if on_delta is not None:
                on_delta('{"verdict":"explore","intents":[]}')
            return PlanResult()

    planner = EmptyPlanner()
    coord = Coordinator(
        board=board,
        planner=planner,
        factory=SimpleNamespace(budget_state=None),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="测试题",
        budget=Budget(),
        workers=2,
        tick=0.005,
        planner_cooldown=0.02,
        interrupt=interrupt,
    )
    task = asyncio.create_task(coord.solve())
    for _ in range(100):
        if planner.calls >= 2:
            break
        await asyncio.sleep(0.005)
    interrupt.set()
    code = await asyncio.wait_for(task, timeout=1)

    assert code == EXIT_ERROR
    assert planner.calls >= 2
    assert any(isinstance(event, ModelRequestStarted) and event.worker == "planner" for event in events)
    assert any(isinstance(event, AgentReasoningDelta) and event.worker == "planner" for event in events)
    assert any(isinstance(event, ModelRequestComplete) and event.worker == "planner" for event in events)
    assert any(
        getattr(event, "kind", "") == "swarm_note" and "重新规划" in event.message
        for event in events
    )
    board.close()


async def test_race_stall_enters_swarm_early_but_interrupt_still_stops():
    from foxdog.core.budget import Budget
    from foxdog.swarm.coordinator import Coordinator

    class StubGoal:
        def __init__(self, reason, code=EXIT_ERROR):
            self.last_stop_reason = reason
            self.code = code
            self.loop = SimpleNamespace(
                budget_state=SimpleNamespace(elapsed_minutes=0.2, total_tokens=123)
            )
            self.budget = None

        async def solve(self, history):
            return self.code

    events = []
    stalled = Coordinator(
        board=None,
        planner=None,
        factory=None,
        emit=events.append,
        rollout=None,
        gate=None,
        objective="题目",
        budget=Budget(),
        race_goal=StubGoal("stalled"),
    )
    code, minutes, tokens = await stalled._race()
    assert code is None and minutes == 0.2 and tokens == 123
    assert any("提前切换 swarm" in event.message for event in events)

    interrupted = Coordinator(
        board=None,
        planner=None,
        factory=None,
        emit=lambda _event: None,
        rollout=None,
        gate=None,
        objective="题目",
        budget=Budget(),
        race_goal=StubGoal("interrupted"),
    )
    code, _minutes, _tokens = await interrupted._race()
    assert code == EXIT_ERROR

    turn_limited = Coordinator(
        board=None,
        planner=None,
        factory=None,
        emit=lambda _event: None,
        rollout=None,
        gate=None,
        objective="题目",
        budget=Budget(max_turns=1),
        race_goal=StubGoal("max_turns", EXIT_BUDGET),
    )
    code, _minutes, _tokens = await turn_limited._race()
    assert code == EXIT_BUDGET
