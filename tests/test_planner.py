"""ReasonPlanner：合法/围栏包裹/非法 JSON 三种解析，verdict fallback，重试一次。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from foxdog.core.goal import EXIT_BUDGET, EXIT_COMPLETE, EXIT_ERROR
from foxdog.llm.client import FakeProvider
from foxdog.swarm.planner import ReasonPlanner, parse_plan
from foxdog.swarm.spectator import Spectator, parse_review

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


def test_parse_spectator_copied_enum_placeholder_infers_continue():
    r = parse_review('{"verdict":"continue|correct|complete","summary":"无新动作","close_intents":[]}')
    assert r.verdict == "continue"
    assert r.summary == "无新动作"


def test_parse_spectator_copied_enum_placeholder_with_actions_corrects():
    r = parse_review(
        '{"verdict":"continue|correct|complete","instruction":"关闭 I3，I6 已覆盖","close_intents":["I3"]}'
    )
    assert r.verdict == "correct"
    assert r.close_intents == ["I3"]


def test_parse_spectator_plain_action_has_teeth():
    r = parse_review("立即关闭/合并 I3、I6：这些意图已经被黑板事实覆盖。")
    assert r.verdict == "correct"
    assert r.close_intents == ["I3", "I6"]
    assert "关闭/合并" in r.instruction


def test_parse_spectator_plain_merge_keeps_first_intent():
    r = parse_review("把 I12/I13/I14 合并为唯一本地 exploit 任务，保留 I12。")
    assert r.verdict == "correct"
    assert r.close_intents == []
    assert r.merge_groups == [["I12", "I13", "I14"]]


def test_parse_spectator_plain_cancel_worker():
    r = parse_review("取消 worker-3：方向已经偏离目标。")
    assert r.verdict == "correct"
    assert r.cancel_workers == ["worker-3"]


def test_parse_spectator_garbage_is_silent_continue():
    r = parse_review("旁观者输出无法解析，保持当前计划")
    assert r.verdict == "continue"
    assert r.summary == ""


def test_spectator_prompt_uses_real_verdict_examples():
    prompt = Spectator.build_prompt("目标", "板", "worker", "recent")
    assert '"continue|correct|complete"' not in prompt
    assert '"verdict":"continue"' in prompt
    assert '"verdict":"correct"' in prompt


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


async def test_spectator_correct_can_close_claimed_intent_and_cancel_worker(tmp_path):
    """旁观者关闭 claimed intent 时先请求交接，宽限期到再强制取消。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.spectator import SpectatorReview

    board = Blackboard(tmp_path / "spectator-close.db")
    ok, iid = board.create_intent("重复分析栈槽", "rev", "local", "rbp-slot")
    assert ok
    assert board.claim_intent("worker-3")["id"] == iid

    class CloseSpectator:
        async def review(self, *_args, **_kwargs):
            return SpectatorReview(
                verdict="correct",
                instruction="关闭 I1：结论已被黑板事实覆盖",
                close_intents=["I1"],
                close_reason="结论已被黑板事实覆盖",
            )

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
        spectator=CloseSpectator(),
        spectator_handoff_grace=0.01,
    )
    task = asyncio.create_task(asyncio.sleep(3600))
    coord._worker_tasks["worker-3"] = task
    coord._worker_intents["worker-3"] = iid

    assert await coord._spectator_once() is None
    assert not task.done()
    assert board.get_intent(iid)["status"] == "claimed"
    await asyncio.sleep(0.05)
    assert task.cancelled()
    await asyncio.gather(task, return_exceptions=True)
    intent = board.get_intent(iid)
    assert intent["status"] == "barren"
    assert "spectator action" in intent["result"]
    assert "未在 0.01s 内完成交接" in intent["result"]
    notes = [getattr(event, "message", "") for event in events]
    assert any("spectator 请求 worker-3 收尾交接" in note for note in notes)
    assert any("spectator 已强制关闭" in note and iid in note for note in notes)
    board.close()


async def test_spectator_graceful_close_collects_worker_handoff(tmp_path):
    """worker 在宽限期内返回时，用无工具总结 turn 交接并写入黑板 result。"""
    from foxdog.core.budget import Budget
    from foxdog.llm.messages import AssistantMessage
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.spectator import SpectatorReview

    board = Blackboard(tmp_path / "spectator-handoff.db")
    ok, iid = board.create_intent("重复 exploit 分析", "pwn", "local", "exploit")
    assert ok
    started = asyncio.Event()
    release = asyncio.Event()

    class HandoffProvider:
        called = False

        async def stream_chat(self, messages, tools, on_delta, **_kwargs):
            self.called = True
            assert tools == []
            assert "旁观者要求收尾交接" in messages[-1]["content"]
            text = "已确认事实：A 可复用\n已排除/失败：B 不成立\n产物路径/命令输出：work/a.txt\n未完成但可能有用的线索：C"
            on_delta(text)
            return AssistantMessage(content=text, usage={"completion_tokens": 5})

    provider = HandoffProvider()

    class HandoffLoop:
        def __init__(self):
            self.provider = provider

        async def run_turn(self, history, turn_no=1):
            started.set()
            await release.wait()
            history.append({"role": "assistant", "content": "主分析：发现 A，写了 work/a.txt"})

    class CloseSpectator:
        async def review(self, *_args, **_kwargs):
            return SpectatorReview(
                verdict="correct",
                close_intents=["I1"],
                close_reason="重复方向，交给 I2",
            )

    events = []
    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None, extra_providers=[], make_loop=lambda wid, provider=None: HandoffLoop()),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False, expected_flags=1, flags=[]),
        objective="审计目标",
        budget=Budget(),
        spectator=CloseSpectator(),
        spectator_handoff_grace=2.0,
    )
    task = asyncio.create_task(coord._run_worker("worker-7"))
    coord._worker_tasks["worker-7"] = task
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await coord._spectator_once() is None
    assert board.get_intent(iid)["status"] == "claimed"
    release.set()
    await asyncio.wait_for(task, timeout=1)

    intent = board.get_intent(iid)
    assert intent["status"] == "barren"
    assert "[worker handoff]" in intent["result"]
    assert "A 可复用" in intent["result"]
    assert provider.called
    notes = [getattr(event, "message", "") for event in events]
    assert any("spectator 优雅关闭" in note and iid in note for note in notes)
    board.close()


async def test_spectator_natural_language_merge_closes_duplicates(tmp_path):
    """旧模型只写自然语言“把 I1/I2 合并”时也做保守执行：保留第一个，关闭其余。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.spectator import SpectatorReview

    board = Blackboard(tmp_path / "spectator-merge.db")
    ok, i1 = board.create_intent("本地 exploit 甲", "pwn", "local", "exploit-a")
    assert ok
    ok, i2 = board.create_intent("远程 exploit 乙", "pwn", "remote", "exploit-b")
    assert ok
    ok, i3 = board.create_intent("符号执行 exploit 丙", "rev", "local", "symbolic-c")
    assert ok

    class MergeSpectator:
        async def review(self, *_args, **_kwargs):
            return SpectatorReview(
                verdict="correct",
                instruction="把 I1/I2/I3 合并为唯一本地 exploit 任务，保留 I1",
            )

    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None),
        emit=lambda _event: None,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="审计目标",
        budget=Budget(),
        spectator=MergeSpectator(),
    )

    assert await coord._spectator_once() is None
    assert board.get_intent(i1)["status"] == "open"
    assert board.get_intent(i2)["status"] == "barren"
    assert board.get_intent(i3)["status"] == "barren"
    board.close()


async def test_spectator_summary_close_clause_has_teeth(tmp_path):
    """旧模型把关闭建议写在 summary 里时，也应真正关闭对应 intent。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.spectator import SpectatorReview

    board = Blackboard(tmp_path / "spectator-summary-close.db")
    ids = []
    for goal in ("rbp 栈槽", "Exchange UAF", "本地 exploit"):
        ok, iid = board.create_intent(goal, "rev", "local", goal)
        assert ok
        ids.append(iid)

    class SummaryOnlySpectator:
        async def review(self, *_args, **_kwargs):
            return SpectatorReview(
                verdict="correct",
                summary="立即关闭/合并 I1、I2：这些意图的结论已在黑板覆盖。",
            )

    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None),
        emit=lambda _event: None,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="审计目标",
        budget=Budget(),
        spectator=SummaryOnlySpectator(),
    )

    assert await coord._spectator_once() is None
    assert board.get_intent(ids[0])["status"] == "barren"
    assert board.get_intent(ids[1])["status"] == "barren"
    assert board.get_intent(ids[2])["status"] == "open"
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


async def test_worker_auto_finish_keeps_long_report_on_board(tmp_path):
    """自动收尾保留较完整报告，事件只显示紧凑摘要，避免 UI 半截 markdown。"""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator

    board = Blackboard(tmp_path / "long-report.db")
    ok, iid = board.create_intent("总结一个复杂方向", "rev", "local", "summary")
    assert ok
    long_report = "I11 已完成并收尾。\n\n## I11 结论\n" + ("细节 " * 500)

    class ReportLoop:
        async def run_turn(self, history, turn_no=1):
            history.append({"role": "assistant", "content": long_report})

    events = []
    coord = Coordinator(
        board=board,
        planner=SimpleNamespace(),
        factory=SimpleNamespace(budget_state=None, extra_providers=[], make_loop=lambda wid, provider=None: ReportLoop()),
        emit=events.append,
        rollout=None,
        gate=SimpleNamespace(solved=False, expected_flags=1, flags=[]),
        objective="审计",
        budget=Budget(),
    )
    await coord._run_worker("worker-11")

    result = board.get_intent(iid)["result"]
    assert "I11 结论" in result
    assert len(result) > 1000
    notes = [getattr(event, "message", "") for event in events]
    done_note = next(note for note in notes if "完成" in note and iid in note)
    assert "\n" not in done_note
    assert "报告已写入黑板" in done_note
    assert len(done_note) < 260
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


async def test_valid_empty_plan_does_not_retry_model_request():
    provider = FakeProvider(script=[("text", '{"verdict":"explore","intents":[],"audit":[]}')])
    planner = ReasonPlanner(provider)
    r = await planner.plan("题目", "黑板摘要")
    assert provider.calls == 1
    assert r.verdict == "explore" and r.intents == []


async def test_plan_gives_up_after_retry():
    provider = FakeProvider(script=[("text", "垃圾1"), ("text", "垃圾2")])
    planner = ReasonPlanner(provider)
    r = await planner.plan("题目", "黑板摘要")
    assert provider.calls == 2 and r.verdict == "explore" and r.intents == []


async def test_coordinator_replans_while_worker_capacity_is_unfilled(tmp_path):
    """A valid empty plan must not strand spare /workers slots behind one long worker."""
    from foxdog.core.budget import Budget
    from foxdog.swarm.blackboard import Blackboard
    from foxdog.swarm.coordinator import Coordinator
    from foxdog.swarm.planner import PlanResult

    board = Blackboard(tmp_path / "planner-refill.db")
    interrupt = asyncio.Event()

    class RefillPlanner:
        calls = 0
        objectives: list[str] = []

        async def plan(self, objective, *_args, **_kwargs):
            self.calls += 1
            self.objectives.append(objective)
            if self.calls == 1:
                return PlanResult.model_validate(
                    {
                        "verdict": "explore",
                        "intents": [
                            {
                                "goal": "长时间验证主路线",
                                "category": "pwn",
                                "surface": "local",
                                "technique": "primary",
                            }
                        ],
                    }
                )
            if self.calls >= 3:
                interrupt.set()
            return PlanResult(verdict="explore", intents=[])

    planner = RefillPlanner()
    coord = Coordinator(
        board=board,
        planner=planner,
        factory=SimpleNamespace(budget_state=None),
        emit=lambda _event: None,
        rollout=None,
        gate=SimpleNamespace(solved=False),
        objective="复杂 pwn 题",
        budget=Budget(),
        workers=3,
        tick=0.005,
        planner_cooldown=0.01,
        interrupt=interrupt,
    )

    async def hold_worker(worker_id: str) -> None:
        intent = board.claim_intent(worker_id)
        assert intent is not None
        coord._worker_intents[worker_id] = intent["id"]
        try:
            await asyncio.Event().wait()
        finally:
            coord._worker_intents.pop(worker_id, None)

    coord._run_worker = hold_worker  # type: ignore[method-assign]
    code = await asyncio.wait_for(coord.solve(), timeout=1)

    assert code == EXIT_ERROR
    assert planner.calls >= 3
    assert "用户上限=3" in planner.objectives[0]
    assert "本轮最多新增=3" in planner.objectives[0]
    board.close()


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
