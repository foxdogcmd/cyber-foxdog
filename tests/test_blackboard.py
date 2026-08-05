"""黑板：并发认领恰好 1 WON、route 折叠、近重复拒绝、barren 二次去重、事件重放重建。"""
from __future__ import annotations

import threading

from foxdog.swarm.blackboard import Blackboard
from foxdog.swarm.facts import FactsBoard


def test_facts_deadends_views(tmp_path):
    bb = Blackboard(tmp_path / "bb.db")
    fid = bb.post_fact("8080 是 nginx", "handle1", actor="w1")
    bb.post_deadend("直接爆破", "全 404", actor="w1")
    bb.post_directive("旁观者纠偏：先验证凭据", actor="spectator", priority="high")
    assert bb.facts()[0]["content"] == "8080 是 nginx"
    assert bb.deadends()[0]["direction"] == "直接爆破"
    assert bb.directives()[0]["content"] == "旁观者纠偏：先验证凭据"
    assert all("旁观者纠偏" not in f["content"] for f in bb.facts())
    assert bb.version() == 3
    assert bb.reject_fact(fid, actor="planner")
    assert bb.facts(status="unresolved") == []
    assert bb.facts()[0]["status"] == "rejected"
    assert not bb.reject_fact(fid)  # 已 rejected 不能重复拒
    bb.close()


def test_concurrent_claim_exactly_one_winner(tmp_path):
    bb = Blackboard(tmp_path / "bb.db")
    ok, iid = bb.create_intent("读 /flag", "file", "local", "read", actor="test")
    assert ok
    won: list[str] = []
    lost: list[str] = []

    def worker(wid: str) -> None:
        got = bb.claim_intent(wid)
        (won if got else lost).append(wid)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1 and len(lost) == 9
    intent = bb.get_intent(iid)
    assert intent["status"] == "claimed" and intent["claimed_by"] == won[0]
    bb.close()


def test_route_collapse_and_barren_dedup(tmp_path):
    bb = Blackboard(tmp_path / "bb.db")
    ok, iid = bb.create_intent("对 8080 跑目录爆破", "web", "tcp:8080", "dir-brute")
    assert ok
    # route 折叠：同 category:surface:technique 有 open intent → 拒
    ok2, reason = bb.create_intent("对 8080 做目录 fuzz", "web", "tcp:8080", "dir-brute")
    assert not ok2 and "折叠" in reason
    # barren 二次去重：标贫瘠后同 route 拒建
    bb.claim_intent("w1")
    assert bb.complete_intent(iid, "barren", "字典全 404", actor="w1")
    ok3, reason3 = bb.create_intent("再试 8080 目录爆破", "web", "tcp:8080", "dir-brute")
    assert not ok3 and "barren" in reason3
    # 不同 technique 不受限
    ok4, _ = bb.create_intent("对 8080 试 SQL 注入", "web", "tcp:8080", "sqli")
    assert ok4
    bb.close()


def test_near_dup_goal_rejected(tmp_path):
    bb = Blackboard(tmp_path / "bb.db")
    ok, _ = bb.create_intent("读取工作区里的 flag.txt 文件内容", "file", "local", "read-a")
    assert ok
    ok2, reason = bb.create_intent("读取工作区里的 flag.txt 文件的内容", "file", "local", "read-b")
    assert not ok2 and "近重复" in reason
    bb.close()


def test_intent_id_has_goal_hash(tmp_path):
    bb = Blackboard(tmp_path / "bb.db")
    ok1, id1 = bb.create_intent("目标甲", "a", "b", "c")
    ok2, id2 = bb.create_intent("目标乙", "x", "y", "z")
    assert ok1 and ok2
    assert id1.startswith("I1-") and id2.startswith("I2-")
    assert id1 != id2 and id1.split("-")[1] != id2.split("-")[1]  # goal 哈希后缀防 I1 撞车
    bb.close()


def test_rebuild_views_replay(tmp_path):
    path = tmp_path / "bb.db"
    bb = Blackboard(path)
    bb.post_fact("f1", "h1", actor="w1")
    bb.post_deadend("d1", "r1", actor="w1")
    bb.post_directive("dir1", actor="spectator", priority="high")
    ok, iid = bb.create_intent("g1", "c", "s", "t", lane="brute:tcp:8080@h")
    assert ok
    bb.claim_intent("w2")
    bb.complete_intent(iid, "done", "拿到结果", actor="w2")
    before = (bb.facts(), bb.deadends(), bb.directives(), bb.intents(), bb.version())
    bb.rebuild_views()  # 全量重放
    after = (bb.facts(), bb.deadends(), bb.directives(), bb.intents(), bb.version())
    assert before == after
    assert bb.get_intent(iid)["status"] == "done"
    bb.close()
    # 重开连接数据仍在（持久化）
    bb2 = Blackboard(path)
    assert bb2.facts()[0]["content"] == "f1"
    bb2.close()


def test_facts_board_facade(tmp_path):
    board = FactsBoard(tmp_path / "blackboard.db")
    board.post_fact("靶机有 .git 泄露", "h1")
    board.post_deadend("目录爆破", "全 404")
    text = board.render()
    assert "已知事实" in text and "靶机有 .git 泄露" in text and "h1" in text
    assert "已排除方向" in text and "目录爆破" in text
    # 摘要（planner/worker 用）含三类对象
    summary = board.summary()
    assert "黑板摘要" in summary and ".git 泄露" in summary and "目录爆破" in summary
    board.post_directive("旁观者纠偏：不要重复爆破", actor="spectator", priority="high")
    text2 = board.render()
    assert "控制指令" in text2 and "旁观者纠偏" in text2
    assert all("旁观者纠偏" not in f["content"] for f in board.facts())
    board.close()


def test_claim_idempotent_for_same_worker(tmp_path):
    """coordinator 代认领后 worker 自己再 claim 不应 LOST（swarm 实战 worker-4 困惑）。"""
    bb = Blackboard(tmp_path / "bb.db")
    ok, iid = bb.create_intent("读 /flag", "web", "/", "file-read", actor="planner")
    assert ok
    first = bb.claim_intent("worker-1")
    assert first and first["id"] == iid
    again = bb.claim_intent("worker-1")  # 重复认领 → 幂等返回同一 intent
    assert again and again["id"] == iid
    assert bb.claim_intent("worker-2") is None  # 别的 worker：open 已空 → LOST
    bb.close()


def test_claim_after_finish_gets_next(tmp_path):
    bb = Blackboard(tmp_path / "bb.db")
    ok, i1 = bb.create_intent("任务一", "web", "/", "t1", actor="p")
    ok, i2 = bb.create_intent("任务二", "web", "/", "t2", actor="p")
    assert bb.claim_intent("w")["id"] == i1
    bb.finish_claimed(i1, "w", "done", "完成")
    assert bb.claim_intent("w")["id"] == i2  # 收尾后认领下一个
    bb.close()
