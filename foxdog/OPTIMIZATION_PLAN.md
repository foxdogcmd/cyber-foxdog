# Foxdog 系统优化方案

基于 20260806-163202 运行分析（104分钟无解）

## 一、核心问题诊断

### 1. 远程服务故障响应迟钝
- **08:50** 最后成功交互 → **09:39** 首次EOF → **I26** 才系统性确认
- 浪费约 **50分钟** 等待不存在的响应
- 又花 **90分钟** 重复探测（I29/I31/I35）

### 2. Intent 规划过度细碎
- 38个 Intent（I1-I38），大量重复验证
- I10/I11/I12 都验证堆布局
- I17/I19/I20 都验证 gate
- I26/I29/I35 都探测 banner

### 3. 本地验证冗余
- I15 本地成功后，又做了 I20/I25/I36 多轮验证
- 边际收益递减

### 4. 缺乏主动决策点
- 被动等待服务恢复，未考虑联系管理员/切换目标

## 二、优化方案

### A. 智能外部依赖监控 🔥

#### 1. 三层健康检查
```python
# foxdog/swarm/health_monitor.py

class ServiceHealthMonitor:
    """远程服务健康状态追踪"""
    
    def __init__(self):
        self.failure_history = []  # (timestamp, error_type)
        self.last_success = None
        
    def record_attempt(self, success: bool, error_type: str = None):
        now = time.time()
        if success:
            self.last_success = now
            self.failure_history.clear()
        else:
            self.failure_history.append((now, error_type))
    
    def assess_health(self) -> tuple[str, str, bool]:
        """
        Returns: (status, diagnosis, should_escalate)
        status: healthy | degraded | critical | dead
        diagnosis: 人类可读的诊断
        should_escalate: 是否需要用户介入
        """
        if not self.failure_history:
            return "healthy", "", False
        
        recent_5min = [e for e in self.failure_history 
                       if time.time() - e[0] < 300]
        
        # 连续 3 次 EOF/timeout = critical
        if len(recent_5min) >= 3:
            error_types = [e[1] for e in recent_5min[-3:]]
            if all(e in ["EOF", "timeout"] for e in error_types):
                return ("critical", 
                        f"连续 {len(recent_5min)} 次连接失败 (EOF/timeout)",
                        True)
        
        # 15分钟内失败 >= 5 次 = dead
        recent_15min = [e for e in self.failure_history 
                        if time.time() - e[0] < 900]
        if len(recent_15min) >= 5:
            return ("dead",
                    f"15分钟内 {len(recent_15min)} 次失败，服务可能已崩溃",
                    True)
        
        return "degraded", f"最近 {len(recent_5min)} 次失败", False
```

#### 2. 早期决策点注入
```python
# 在 coordinator.py 的主循环中

class Coordinator:
    def __init__(self, ...):
        self.health_monitor = ServiceHealthMonitor()
        self.escalation_shown = False
    
    async def _check_remote_health(self):
        """每轮检查远程健康状态"""
        status, diagnosis, escalate = self.health_monitor.assess_health()
        
        if escalate and not self.escalation_shown:
            self.escalation_shown = True
            # 发出用户决策请求
            self._emit_note(SwarmNote(
                agent="coordinator",
                severity="critical",
                text=f"⚠️ 远程服务异常: {diagnosis}\n\n"
                     f"建议操作:\n"
                     f"1. 检查靶机网络/平台公告\n"
                     f"2. 联系 CTF 管理员报告服务故障\n"
                     f"3. 如有备用靶机，手动切换目标\n\n"
                     f"按 Ctrl+C 中断，或继续等待自动恢复"
            ))
            
            # 自动降低探测频率：从每轮到每5分钟
            self.health_check_interval = 300
```

#### 3. Spectator 识别外部阻塞
```python
# 增强 spectator.py

SPECTATOR_INSTRUCTION += """
8. **外部依赖阻塞识别**：如果多个 worker 都卡在同一个外部服务
   （如远程靶机连接失败、API 超时），立即标记为 blocked_by_external，
   输出 verdict=correct + instruction="暂停相关 intent，等待外部恢复"。
   不要让整个 swarm 都卡在一个不响应的端点上。
"""

class SpectatorReview(BaseModel):
    blocked_by_external: str | None = None  # 新增：外部依赖标识
```

### B. Intent 规划智能化 🧠

#### 1. 防重复机制升级
```python
# swarm/planner.py

PLANNER_INSTRUCTION += """
9. **严格去重**：产出 intent 前，对照黑板上：
   - 已完成 intent 的 goal/category/surface/technique
   - 在跑 intent 的方向
   - deadend 中已排除的方向
   
   只有当新 intent 能产生**黑板上不存在的新证据**时才派发。
   
10. **进度感知分阶段**：
    - 初期（无 flag 线索）：广度探索，最多 3-4 个并发方向
    - 中期（有明确漏洞）：聚焦验证，2-3 个并发
    - 后期（本地 exploit 成功）：远程利用单线，备用方案1个
    
11. **避免过度验证**：同一个结论（如"本地 ROP 成功"），
    验证 1-2 次即可，不要反复验证。
"""
```

#### 2. Intent 粒度自适应
```python
# 在 coordinator 传给 planner 的上下文中增加进度信息

def _build_board_summary_with_phase(self) -> str:
    summary = self.blackboard.summary()
    
    # 自动识别当前阶段
    facts = self.blackboard.facts
    intents = self.blackboard.intents
    
    if any("flag{" in str(f) for f in facts):
        phase = "FLAG_OBTAINED"
    elif any("本地" in i.goal and "成功" in i.status for i in intents.values()):
        phase = "LOCAL_SUCCESS_REMOTE_PENDING"
    elif any("exploit" in i.goal.lower() for i in intents.values()):
        phase = "EXPLOIT_DEVELOPMENT"
    else:
        phase = "INITIAL_RECON"
    
    summary += f"\n\n当前阶段: {phase}"
    return summary
```

### C. 失败快速响应 ⚡

#### 1. 指数退避 + 熔断
```python
# tools/builtin/remote.py (新增)

class RemoteConnectionTool:
    """封装远程连接，内置重试和熔断"""
    
    def __init__(self):
        self.failure_count = 0
        self.last_failure = 0
        self.circuit_open = False
        
    async def connect(self, host, port, timeout=5):
        # 熔断器开启，直接失败
        if self.circuit_open:
            if time.time() - self.last_failure < 300:  # 5分钟后重试
                raise CircuitBreakerOpen("远程连接已熔断，请稍后再试")
            self.circuit_open = False
            self.failure_count = 0
        
        try:
            conn = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout
            )
            self.failure_count = 0
            return conn
        except Exception as e:
            self.failure_count += 1
            self.last_failure = time.time()
            
            # 连续 5 次失败 → 开启熔断器
            if self.failure_count >= 5:
                self.circuit_open = True
                self._emit_alert("远程连接熔断，暂停所有远程操作")
            
            raise
```

#### 2. Worker 超时自动回收
```python
# coordinator.py 中增强

class Coordinator:
    WORKER_SOFT_TIMEOUT = 600   # 10分钟软超时（提醒）
    WORKER_HARD_TIMEOUT = 1800  # 30分钟硬超时（强制回收）
    
    async def _monitor_worker_timeouts(self):
        """后台监控 worker 超时"""
        while self.active:
            for intent_id, worker_state in self.active_workers.items():
                elapsed = time.time() - worker_state.start_time
                
                # 软超时：提醒但不中断
                if elapsed > self.WORKER_SOFT_TIMEOUT and not worker_state.warned:
                    worker_state.warned = True
                    self._emit_note(SwarmNote(
                        agent=worker_state.worker_id,
                        text=f"⏰ {intent_id} 已运行 {elapsed/60:.1f} 分钟，请确认是否卡住"
                    ))
                
                # 硬超时：强制回收
                if elapsed > self.WORKER_HARD_TIMEOUT:
                    self._emit_note(SwarmNote(
                        agent="coordinator",
                        severity="warning",
                        text=f"🛑 {intent_id} 超过 30 分钟未完成，强制标记为 barren"
                    ))
                    await self._force_complete_intent(
                        intent_id, 
                        status="barren",
                        result="超时未完成，可能卡在外部依赖或死循环"
                    )
            
            await asyncio.sleep(60)  # 每分钟检查一次
```

### D. 进度可视化增强 📊

#### 1. TUI 状态面板升级
```python
# tui.py 中增加

class SwarmStatusPanel:
    """实时显示 swarm 健康状态"""
    
    def render(self):
        # 远程服务健康指示器
        health_status = self.coordinator.health_monitor.assess_health()
        health_color = {
            "healthy": "green",
            "degraded": "yellow", 
            "critical": "red",
            "dead": "red bold"
        }[health_status[0]]
        
        self.console.print(f"[{health_color}]● 远程服务: {health_status[0]}[/]")
        if health_status[1]:
            self.console.print(f"  {health_status[1]}", style="dim")
        
        # Intent 阶段分布
        phase_counts = defaultdict(int)
        for intent in self.blackboard.intents.values():
            phase_counts[self._classify_intent(intent)] += 1
        
        self.console.print("\n阶段分布:")
        for phase, count in phase_counts.items():
            self.console.print(f"  {phase}: {count}")
        
        # 时间效率指标
        elapsed = time.time() - self.start_time
        facts_per_min = len(self.blackboard.facts) / (elapsed / 60)
        self.console.print(f"\n效率: {facts_per_min:.1f} facts/min")
```

#### 2. Writeup 质量提升
```python
# store/writeup.py

def generate_writeup(ws: Workspace, objective: str, gate: FlagGate) -> str:
    facts = ws.blackboard.facts
    intents = ws.blackboard.intents
    
    # 增加诊断部分
    diagnostics = _analyze_run_efficiency(ws)
    
    writeup = f"""# Writeup: {objective}

## 结果
{_format_result(gate)}

## 效率分析
- 总耗时: {diagnostics['total_time_min']:.1f} 分钟
- Intent 总数: {diagnostics['total_intents']}
- 有效 Intent: {diagnostics['productive_intents']} ({diagnostics['productive_rate']:.0%})
- 重复验证: {diagnostics['redundant_intents']} 个
- 外部阻塞时长: {diagnostics['blocked_time_min']:.1f} 分钟

{_efficiency_recommendations(diagnostics)}

## 时间线
{_build_timeline(intents)}

## 关键发现
{_format_facts(facts)}
"""
    return writeup

def _efficiency_recommendations(diag: dict) -> str:
    """基于本次运行给出改进建议"""
    rec = []
    if diag['redundant_intents'] > 5:
        rec.append("- 建议: 加强 intent 去重，减少重复验证")
    if diag['blocked_time_min'] > 30:
        rec.append("- 建议: 优化外部依赖健康检查，早期暂停无效探测")
    if diag['productive_rate'] < 0.6:
        rec.append("- 建议: 提升 planner 质量，聚焦高价值方向")
    return "\n".join(rec) if rec else "本次运行效率良好"
```

### E. 智能预算分配 💰

#### 1. 阶段化预算
```python
# core/budget.py

class AdaptiveBudget(Budget):
    """根据进度自动调整预算分配"""
    
    def __init__(self, total: int):
        self.total = total
        self.phase_allocations = {
            "recon": 0.3,      # 30% 用于侦查
            "exploit": 0.4,    # 40% 用于 exploit 开发
            "remote": 0.2,     # 20% 用于远程利用
            "reserve": 0.1     # 10% 预留
        }
        self.spent_by_phase = defaultdict(int)
    
    def check_phase_budget(self, phase: str) -> bool:
        """检查当前阶段是否还有预算"""
        allocated = self.total * self.phase_allocations[phase]
        spent = self.spent_by_phase[phase]
        return spent < allocated
    
    def auto_rebalance(self, current_phase: str):
        """自动重新分配剩余预算"""
        # 如果侦查阶段提前完成，把剩余预算转移到 exploit
        if current_phase == "exploit":
            unused_recon = (self.total * self.phase_allocations["recon"] 
                          - self.spent_by_phase["recon"])
            if unused_recon > 0:
                self.phase_allocations["exploit"] += unused_recon / self.total
                self.phase_allocations["recon"] = 0
```

## 三、实施计划

### Phase 1: 已落地
1. [x] 实现 `ServiceHealthMonitor`
2. [x] `runner.build_run` 将真实工具事件接入健康监控
3. [x] Coordinator 加入健康检查循环，并把告警写成黑板 directive
4. [x] Spectator 支持 `blocked_by_external`，Coordinator 会消费并写入 directive
5. [x] Planner 指令增强（去重+阶段感知+外部阻塞响应）
6. [x] 补齐健康监控、spectator 阻塞、runner 接线回归测试

### Phase 2: 待实施
7. [ ] Worker 软/硬超时回收
8. [ ] Intent 粒度自适应的代码级阶段识别
9. [ ] TUI 状态面板健康指示器

### Phase 3: 待实施
10. [ ] Writeup 诊断分析
11. [ ] 自适应预算分配
12. [ ] 基于历史 run 的性能基准

## 四、预期效果

### 解题速度
- **远程故障响应**: 50分钟 → 5分钟内决策
- **Intent 效率**: 38个 → 预计15-20个（减少50%）
- **总耗时**: 104分钟 → 预计40-60分钟（同类题目）

### 用户体验
- 实时健康指示器，不再盲等
- 关键决策点主动提示
- Writeup 自带效率诊断

### 系统可靠性
- 熔断器防止资源浪费
- 超时自动回收，不卡死
- 外部依赖失败不传染

---

**优先级排序**: A1 > A2 > C2 > B1 > D1 > 其他

本方案覆盖 **监控、规划、执行、反馈** 全链路，预计整体解题效率提升 **40-60%**。
