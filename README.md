# foxdog

CTF 统一解题平台（交互式会话 TUI）。终端裸输 `foxdog` 进入持续会话：像聊天一样下 CTF 任务、中途 steer 指挥、连续解多题；也有 headless `solve` 供脚本编排。拿到 flag 经 FlagGate 三重校验后输出（可自动提交比赛平台）；全程事件溯源落盘，可复盘。

## 快速开始

```bash
uv venv .venv && uv pip install -e .        # Python ≥ 3.11
foxdog                                       # 纯净启动，零预配置
# 首次使用：/model 配置模型（菜单选协议 openai/openai-responses/anthropic → 选预设（Kimi/DeepSeek）或全手动 → key 不回显 → 自动测连接 → 落盘；Esc 任意步取消）
```

```
┌ foxdog | deepseek-v4-flash | yolo | workers=3 | 空闲 ───┐
│ 🦊 foxdog — CTF 统一解题平台                              │
│ > 帮我打 http://target:8080 这道题，flag 格式 PCTF{...}   │
│ ▶ http_request ... ✓ (812ms)                            │
│ FLAG VERIFIED: PCTF{...}                                │
│ ⚑ flag 已自动提交平台（HTTP 200）                         │
│ ✔ 解题成功（用时 3m12s，182k tokens）writeup: ...         │
│ > _              ← 继续下任务；解题中直接打字 = steer      │
└──────────────────────────────────────────────────────────┘
```

斜杠命令（输 `/` 弹自动补全：候选+描述，↑↓ 选择，Tab/Enter 补全，Esc 关闭）：

| 命令 | 说明 |
|---|---|
| `/model` | 查看/菜单式配置模型（openai chat / openai responses / anthropic 三协议 + Kimi·DeepSeek 预设，连接测试后落盘 0600，热切换；Esc 任意步取消）；`/model list` 只看当前 |
| `/setting [要求]` | 独立设置页的全局 copilot：自然语言配置与指挥，多轮连续对话（Esc 返回、历史重进不丢）；解题中也可用（走 planner 模型）。配置 agent 只有配置/指挥工具，无 shell/文件，物理隔离 |
| `/budget` | 预算默认无限；`/budget minutes 30` / `tokens 500000` / `off`，运行中立即生效 |
| `/fetch <id\|URL>` | 拉题目描述进输入框（用平台 fetch_url 的 {id} 占位，或直接抓 URL） |
| `/board` | 全屏黑板视图（再按或 Esc 返回） |
| `/mode yolo\|plan` | 切权限模式 |
| `/workers N` | 下一任务 worker 数 |
| `/target` `/format` | 下一任务靶机 / flag 格式正则 |
| `/runs` `/new` `/quit` | 历史 run / 清历史新会话 / 退出 |
| Enter | 提交（多行内容连换行一起提交） |
| Shift+Enter / Ctrl+J | 输入框内换行 |
| Esc | 解题中按 Esc 中断当前任务 |
| Ctrl+B | 将当前正在等待的 `shell_exec` 转入后台；进程继续运行，完成后自动通知 AI |
| Ctrl+C | 有选区复制；无选区时运行中单次中断、空闲时 2 秒内连按两次退出（始终走清理流程） |
| Ctrl+T | 任务清单区显隐（✓ 已完成灰 / ■ 进行中黄 / □ 待办） |
| Ctrl+End | 回到底部（上翻时右下角有 Jump to bottom 提示，点击同效） |

交互细节：文本可拖选复制；输入区参考 Kimi Code 使用圆角线框（多行 TextArea：Enter 提交、Shift+Enter/Ctrl+J 换行，高度随行数自适应、上限 10 行、超出内部滚动，soft_wrap 无横向滚动条；超过 200 字符的粘贴折叠为蓝色占位符 `[粘贴的文本#N]`、提交时自动展开还原），提示符和软件强调色竖线光标位于框内，先清除 Textual 原生反色块再插入，不遮字符且 placeholder 不跳；启动即聚焦输入框，窗口回焦（AppFocus）、补全选择后、/setting 返回后、任务结束后都自动落焦输入框；活动状态独占框外一行，快捷状态位于框下，底部仍只有一个 composer dock；Windows 下将方向键 CSI 拼接窗口扩到 350ms，并使用 `1002 + 1006` 鼠标模式保留点击/拖动、关闭无按钮悬停上报，避免 `[<...M` 和 `[C` 泄漏进输入框；命令输出优先严格 UTF-8 解码，失败时回退 Windows OEM/ANSI 代码页，跨分块字符和混合编码流不会产生乱码，CAS 全文统一存 UTF-8；顶部为单层状态栏，并以青色常驻显示当前实际工作的引擎，异构 race 中会显示类似 `deepseek-v4-flash + Claude Code`，引擎结束后即时移除；用户消息是整行反色横条；agent 文本 `●` 前缀、内联代码橙色、粗体保留；普通工具行保持紧凑折叠，`write_file` 默认展示路径和前 10 行内容，`edit_file` 默认展示 unified diff，剩余行显示 `+N lines` 并可展开；工具成功保持绿色、失败保持红色；任务活动动画仅在运行时刷新，空闲暂停；黑板面板三节（facts/deadends/intents）尾部折叠 +N more，intents 按状态分组（⧗ claimed / ○ open / ✓ done / ✗ barren）。空闲和窄屏保持单列时间线，宽屏 swarm 运行时显示侧栏，`/board` 可随时全屏查看。

turn 边界：一次 turn 会持续进行模型 step 与工具调用，直到模型返回正常的非空最终文本，或遇到 solved/error/interrupt/budget。`max_steps` 只是单个 turn 的工具步分片上限，达到后目标仍 active，会立即开启下一 turn；turn 之间没有固定 sleep，实际空档通常是下一次模型请求的首 token 延迟。`length/max_tokens` 或 thinking-only 不再算正常结束：FoxDog 会在同一 turn 内有限恢复并强制下一步调用工具；完全空响应会有限重试且不写入 history，连续失败明确报错，避免 `assistant:null` 空转。连续 3 个 turn 没有工具进展会标记 `stalled`，多 worker race 会提前切入 swarm 重新规划。若当前只需等待后台任务，FoxDog 会等任务完成、steer 或 Esc，不用模型循环轮询；后台完成结果自动进入 AI 上下文和 CAS 证据链。

会话模型：**一段持续对话历史 + 每次任务一个独立 run**——Workspace/FlagGate/黑板题目级隔离，agent 却记得上一题怎么打的；provider/权限链/env/技能/MCP 会话级共享。

M1 范围：protocol + agent loop + LLM 客户端（OpenAI 兼容，主备故障转移）+ shell/文件/python_eval/http 工具 + YOLO/Plan 权限 + FlagGate + headless solve + rollout。

M2 新增：

- **修复**：`http_request` 默认跳过 TLS 证书验证（`verify` 参数，CTF 自签证书常见）；**step 级预算强制**（minutes/tokens 在 loop 每个 step 前检查，超限抛 `BudgetExceeded` → exit 2，不再"单 turn 无限跑"）；**环境感知系统提示**（OS/shell/python 命令/tmp 目录/已装安全工具注入 prompt，Windows 上不再瞎写 `python3`、`/tmp`、`sh`）。
- **YAML 外部工具配方**（`foxdog/recipes/`）：内置 20+ 个 Web/取证/Pwn/APK 配方（nmap/ffuf/gobuster/sqlmap/hydra/john/hashcat/binwalk/nikto/dirsearch/feroxbuster/nuclei/jadx/apktool 等），`shutil.which` 检测未安装自动跳过；用户配方放 `~/.config/foxdog/recipes/*.yaml`。执行走 `run_argv` 数组传参（不经 shell 拼接）。配方不进默认工具表——`select_tools()` 看目录、`select_tools(names=[...])` 激活完整 schema、`recipe_help(name)` 看详情（渐进披露，省 token）。
- **PowerShell 7 默认执行器**：Windows 的 `shell_exec`、后台命令和 `shell_session` 统一使用 `pwsh`（无 profile、非交互）；持久会话保持目录/变量状态并使用 UTF-8 输出。POSIX 仍用 `sh`/`bash`。
- **上下文压缩**（`core/compaction.py`）：微压缩把超 6 步且 >1KB 的旧工具输出替换为 `[已归档 handle=xxx 摘要]`（全文在 CAS）；估算 token ≥ 窗口 85% 时用 planner 模型（无配置用 solver）按 CTF 专用指令全量压缩，压缩后重渲染 todo + 事实板回上下文。
- **技能系统**（`foxdog/skills/`，提前自 M4）：`manifest.yaml` / SKILL.md frontmatter 声明式知识包，系统提示只列 name+description，`activate_skill(name)` 注入手册全文，`activate_skill(name, resource=...)` 按需读取技能相对资料。内置基础实战手册：`php-file-write-waf`（含 PHP session upload progress 竞争包含完整 exploit 模板）、`web-recon`、`sqli-basics`，并内置固定 commit 的 `reverse-skill` 多技能包（APK/逆向/pwn/取证/渗透等 42 个入口，共享 references/scripts/field-journal）。用户技能放 `~/.config/foxdog/skills/<name>/`，多技能包会整体导入以保留相对资源路径；另内置 `secskills-ctf`（CTF 全类别技能包）和 `semgrep-code-audit`（Semgrep 源码安全扫描）。
- **轻量事实板**：`post_fact` / `post_deadend` 上板，每 turn 开始摘要（各最近 15 条）注入系统提示"已知事实/控制指令/已排除方向"——写死路和写事实一样有价值；操作员/旁观者纠偏走控制指令，不污染事实。
- **writeup 生成**：solve 结束（无论成败）基于 rollout + 黑板生成 `writeup.md` 骨架（目标/时间线/事实/死路/intents/flag）。

M3 新增（多 worker 协作层）：

- **黑板**（`swarm/blackboard.py`）：每 run 一个 SQLite（WAL）`blackboard.db`，事件溯源——append-only `events` 表是唯一事实源，`facts`/`intents`/`deadends` 物化视图可 `rebuild_views()` 重放重建。Intents 带 `route_hash` 去重三件套（route 折叠 / difflib≥0.9 近重复 / barren 二次去重），id 为 `I<n>-<goal sha1前6>` 防撞车；`claim_intent` 原子认领（`UPDATE ... WHERE status='open'` + rowcount 守护，WON/LOST）。M2 的 facts.jsonl 已迁移进黑板（FactsBoard 门面保持每 turn 注入系统提示）。
- **子 agent 工具**：`spawn_agent(prompt)` 扇出独立 loop（共享黑板/FlagGate/run 工作区/权限链，默认 cwd 隔离到 `work/.workers/<id>`，registry 剥掉 spawn 系→深度上限 1），`wait_agent(ids, timeout_s)` 收汇报，`list_agents` 看状态。任一 worker 过闸全员自然停。
- **ReasonPlanner**（`swarm/planner.py`）：读黑板摘要出严格 JSON（verdict + ≤4 intents + audit），围栏容忍/解析失败重试一次/非法 verdict fallback explore。
- **协调主循环**（`swarm/coordinator.py`）：`--workers N` 开启。race 快路径（主 worker 最多冲刺 3 分钟，出 flag 直接结束；provider error 或连续无工具进展则提前切入 swarm，用户中断仍立即终止）→ 2s 一拍 OODA：黑板有变化且过 10s 冷却 → planner 派 intent；空槽有货按批次补 worker。预算由 swarm 整体共享。配套工具 `list_board` / `claim_intent` / `complete_intent`。
- 事件带 `[worker-x]` 前缀（headless 渲染与 JSONL 都有 `worker` 字段）；writeup 纳入 intents 完成情况。

M4 新增（交互与生态）：

- **Steer 中途指挥**（`core/steer.py`）：解题随时插话。headless tty 下直接打字回车（后台线程读 stdin），TUI 用底部输入框；GoalMode 每 turn 开始、Coordinator 每拍 drain 注入（swarm 模式下 steer 作为控制指令上黑板，planner 和后续 worker 都看得到）。rollout 记 `steer` 类型。
- **Textual TUI**（`foxdog/tui.py`，`--tui` 开启）：左侧事件时间线（流式文本、工具调用单行 `▶ name args → ✓/✗ (ms)`、swarm_note、worker 前缀）、右侧黑板面板（facts/deadends/intents 三栏 2s 刷新）、顶部状态行（run_id/用时/tokens/workers/权限模式/flag）、底部输入框（回车 steer，`/mode yolo|plan` 切权限，`q` 退出）。与 headless 共用 `runner.build_run/execute_run` 装配线，不加 `--tui` 时行为与纯 headless 完全一致（textual 懒加载）。
- **MCP client**（`foxdog/mcp_client.py`）：`config.toml [mcp_servers.<name>]` 配 stdio（command/args/env）或 streamable HTTP（url）；启动并发连接，单 server 失败不致命（30s 超时）；MCP 工具走 select_tools 同款渐进披露（`mcp__<server>__<tool>` 前缀防撞名），激活后代理 call_tool，结果超阈值走 CAS spill。`foxdog mcp` 子命令列 server/工具调试。未配置时零开销（懒加载 import）。

M5 新增（交互式会话）：

- **裸 `foxdog` / `foxdog chat` 进交互式会话 TUI**（`foxdog/interactive.py`）：持续对话历史 + 每次任务独立 run（runner.SessionContext 会话级共享 provider/权限/env/技能/MCP；history 跨任务延续）。解题中输入即 steer；任务结束打摘要行（用时/tokens/writeup/exit 语义）回空闲态。provider 未配置时正常启动并打配置指引，提交任务友好报错不崩溃。
- `foxdog solve ...` headless 路径保持不变（脚本编排/批量刷题用）。

平台升级（统一解题平台）：

- **Anthropic 协议**（`llm/anthropic_client.py`）：`[model.solver] protocol = "openai" | "anthropic"`；OpenAI wire ↔ Anthropic Messages 全量转换（system 抽顶层、tool_result 邻接合并、连续同角色合并），流式 tool_use 累积，重试/主备策略对齐 LLMClient。
- **可写配置**（`config.ConfigStore`）：tomllib 读 + 手写两级 TOML 序列化写，原子 tmp+replace 落盘、权限 0600；`api_key` 展示一律脱敏 `***`。新增 `[platform]` 段（fetch_url/submit_url/flag_format/default_workers）。
- **`/model`**：菜单式配模型——协议三选一（openai chat / openai responses / anthropic），预设（Kimi 开放平台、Kimi Code、DeepSeek OpenAI/Anthropic 兼容）或全手动；key 输入不回显、自动测连接、成功才落盘并热切换会话 provider；Esc 任意步取消。Responses API 客户端见 `llm/responses_client.py`（message/function_call/function_call_output 转换 + 流式事件 + usage）。
- **`/setting`**：独立设置页对话式配置（esc 返回主界面；多轮历史 session 级持久，重进不丢）；配置 agent 无 shell/文件/任意网络工具，只能额外读取 DeepSeek 官方 `/models` 列表。
- **命令自动补全**：输 `/` 弹候选列表（注册表驱动，补全与执行共用一份定义），↑↓/Tab/Enter/Esc。
- **平台闭环**：`submit_url` 配置后 flag 过闸自动 POST（5s 超时，失败只警告）；`/fetch` 拉题进输入框；Esc 中断当前任务。

## 安装

```bash
# Python ≥ 3.11
uv venv .venv && uv pip install -e .        # 或 python -m venv .venv 后 pip install -e .
uv pip install -e ".[dev]"                   # 开发依赖（pytest）
```

## 配置（DeepSeek 为例）

分层加载：内置默认 < `~/.config/foxdog/config.toml` < 项目 `./foxdog.toml` < 环境变量 < CLI `-c key=value`。

```toml
# ~/.config/foxdog/config.toml
[model.solver]
provider = "openai-compatible"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"
api_key_env = "DEEPSEEK_API_KEY"
thinking = "enabled"          # 可省略：DeepSeek 当前默认开启思考模式
reasoning_effort = "high"     # low | medium | high | xhigh | max；当前默认 high
# [model.backup]   # 可选：备用端点，主端点连续失败 2 次自动切换
# [model.planner]  # 可选：压缩/摘要专用便宜模型，缺省用 solver

[agent]
mode = "yolo"        # yolo | plan（只读）
max_steps = 200             # 单个 turn 的工具步分片上限，不是整场解题上限
context_window = 128000   # 模型窗口（token），压缩阈值 85%

[budget]
# 默认全不限（注释掉即无限）；交互会话里 /budget 动态调
# max_minutes = 60
# max_tokens = 1000000
# max_turns = 50

# 工具执行环境（可选；默认 local 完全保持宿主执行）
# [runtime]
# kind = "docker"              # local | docker(每题临时容器) | docker-persistent(常驻 foxdog-kali)
# image = "foxdog-runtime:latest"
# network = "bridge"
# auto_build = true       # 镜像缺失时构建内置的无凭据 Kali/CTF 镜像
# auto_remove = true      # 每题结束删除该 run 的容器（docker-persistent 恒为 false）
# workdir = "/workspace/work"
# mounts = ["C:/ctf-data:/data:ro"]  # 仅显式挂载；不会自动挂 home、凭据或 docker.sock

# MCP server（可选）：stdio 或 streamable HTTP，工具经 select_tools 渐进披露
# [mcp_servers.kb]
# command = "python"
# args = ["-m", "my_kb_server"]
# [mcp_servers.web]
# url = "http://127.0.0.1:9000/mcp"

# Anthropic 协议端点（/model 里选 anthropic 也会写成这样）
# [model.solver]
# protocol = "anthropic"
# base_url = "https://api.anthropic.com"   # 可省（官方端点）
# api_key = "sk-ant-..."
# model = "claude-sonnet-4-5"

# 统一解题平台（每场比赛不同，/setting 可对话式修改）
# [platform]
# fetch_url = "https://ctf.example.com/api/challenge/{id}"   # 题目拉取（{id} 占位）
# submit_url = "https://ctf.example.com/api/submit"          # flag 过闸自动 POST {flag, run_id}
# flag_format = 'PCTF\{[^{}]*\}'
# default_workers = 3
```

DeepSeek 思考模式下 foxdog 不发送无效的 `temperature`，会收集流式
`reasoning_content` 并在工具调用的下一步原样回传；Anthropic 协议还会按原顺序保留
多段 signed/redacted `thinking` block 和原始停止原因，并把未显式配置的单次输出上限提高到 32768，
避免默认 4096 被思考 token 耗尽后只得到空正文。turn 用量同时保留缓存命中、
缓存未命中和推理 token。SDK 内层重试已关闭，由 foxdog 统一处理 429、5xx 和连接错误，
避免两层指数退避叠加。
使用 `/setting` 配置 DeepSeek 时，copilot 会先调用官方 `GET https://api.deepseek.com/models`
读取当前 key 的实时可用模型，再调用 `set_model` 落盘，不依赖提示词里的静态模型名。

环境变量兜底（测试/临时用）：`FOXDOG_PROVIDER` / `FOXDOG_BASE_URL` / `FOXDOG_MODEL` / `FOXDOG_API_KEY` / `FOXDOG_RUNTIME`。
无 key 冒烟：`FOXDOG_PROVIDER=fake` 走内置脚本化 provider（读取预置目标输出 → submit_flag → 结束）。

## 异构引擎竞速（race）

swarm 模式（`--workers N > 1`）的 race 阶段并行扇出多个异构引擎，**first-valid-flag-wins**（任一引擎过闸立即取消其余）：

- 内置 solver × 1（主模型）
- 每个 `[model.extra.<name>]` 端点 × 1（独立内置 loop，共享黑板/FlagGate/工作区）
- Claude Code CLI × 1（PATH 探测到 `claude` 才参与）

```toml
# 额外引擎端点（/setting 里 set_model role=extra:<name> 也能配）
[model.extra.kimi]
protocol = "openai"          # 或 anthropic
base_url = "https://api.moonshot.cn/v1"
api_key = "sk-..."
model = "kimi-k2"

# 引擎开关与上限
[agent]
max_race_engines = 3         # 超出按配置顺序截断

[engines]
enabled = true               # 总开关（--no-engines 可临时关）
[engines.claude_code]
enabled = false              # 默认关闭；由 /setting 检查配置后显式开启
containerized = true         # 默认只在 Docker 工具 runtime 中运行
import_host_config = false   # /setting 可开启；每次 run 从 CC Switch 配置动态注入
```

Claude Code CLI 已固定版本安装进 FoxDog 工具镜像，但 race 引擎默认关闭，未启用时不会探测容器、读取本机配置或启动 Claude。使用 `/setting 帮我启用容器 Claude Code 并注入本机 CC Switch 配置` 后，配置副驾会检查配置并同时开启 `enabled` 与 `import_host_config`。它只在 `workers > 1` 的 race 阶段参与，默认 `containerized=true`；`runtime=local` 时会明确跳过，不会静默启动宿主 CLI。每次 run 启动时重新读取本机 `~/.claude/settings.json`，只把 Anthropic 鉴权/端点/模型映射与 `CLAUDE_CODE_*` 白名单变量注入该次 `docker exec`，值不写入 FoxDog 配置或日志。

headless 启动形态是 `claude -p <prompt> --output-format stream-json --verbose --safe-mode --no-session-persistence --dangerously-skip-permissions`。`--safe-mode` 禁止自动加载题目工作区中的 `CLAUDE.md`、hooks 和插件，race 会话也不写进容器 home。危险模式在容器内仍有风险：Claude 可执行镜像内任意命令、使用网络并修改共享 `/workspace`，但看不到宿主 home，也没有挂 Docker socket。显式设置 `containerized=false` 才会兼容旧的宿主 CLI 方式；此时 scratch cwd 不是沙箱，Claude 能访问宿主绝对路径和命令，风险明显更高。任何模式都不把 CC Switch token、历史或 session 数据烘进镜像层。收割协议仍为 `FOUND_FLAG=<flag>` + FlagGate 四重校验，race 胜出或 Ctrl+C 会取消容器内完整进程组。

## Docker 工具环境

Docker 模式容器化命令执行与 Claude Code CLI，不把 FoxDog 主 agent/TUI 塞进容器：模型请求、文件工具、CAS、FlagGate 与协作黑板仍留在宿主；`shell_exec`、后台命令、YAML recipe、`python_eval`、`shell_session` 和 Claude race 在 Linux 容器里执行。run 目录绑定到 `/workspace`，主 solver 默认命令目录是 `/workspace/work`，swarm/子 agent worker 默认命令目录是 `work/.workers/<id>` 对应的容器路径；Claude 的 CC Switch 白名单环境只注入 Claude 进程。

容器内的 agent 使用说明见 `/opt/foxdog/AGENTS.md`，仓库对应文件是 [`runtime_image/AGENTS.md`](foxdog/runtime_image/AGENTS.md)。

```bash
# 可提前构建；否则首次 Docker 任务会自动构建
foxdog runtime-build

# 每题一个临时容器（run 结束清理）
FOXDOG_RUNTIME=docker foxdog

# 常驻全功能 Kali 容器（多题共享，手感=一台常开的 Kali 机）
FOXDOG_RUNTIME=docker-persistent foxdog
foxdog kali start    # 创建/复用 foxdog-kali（镜像缺失自动构建）
foxdog kali status   # 状态/镜像/挂载
foxdog kali exec     # 进交互 bash；foxdog kali exec nmap -sV x 跑单条
foxdog kali stop     # stop + rm
foxdog kali reset    # rm 后重建（清空容器内状态）
```

`docker-persistent`：常驻容器 `foxdog-kali`（label `foxdog.kali=true`，不加 --rm），绑定 `.foxdog-runs/` 根目录到 `/workspace`，每个 run 用独立 `/workspace/<run-id>/work`；run 结束只 pkill 收尾本 run 的 exec，不停容器。

**两端路径约定**（docker 模式系统提示会自动注入）：run 目录 ↔ 容器 `/workspace`（同一批文件，bind mount）；文件工具（`read_file`/`write_file`/`edit_file`）只能读写这个工作区——与 shell 所见完全同一视图，宿主工作区外的文件读写都拒绝（唯一入口 `import_file`）；`shell_exec`/`python_eval`/`shell_session`/配方工具都在容器内执行，路径用 `/workspace/...`。swarm worker 的相对路径默认在自己的 `work/.workers/<id>`，Docker 下 `TMPDIR/TMP/TEMP` 也指向私有 `.tmp`；要共享文件就显式写到公共 `work/`、`inputs/` 或绝对 `/workspace/...`。Windows 上的题目附件（桌面/下载）先用 `import_file` 复制进 `inputs/`——它返回 workspace 相对路径和容器内绝对路径，别猜两端路径。

容器取消时 FoxDog 会终止本次命令的容器内进程组（procps pkill -g），run 结束再统一清理（临时模式删容器，常驻模式只收尾 exec）。

## 用法

```bash
# 主推：交互式会话（持续对话，连续解多题，解题中打字 = steer）
foxdog
foxdog chat            # 显式入口，同上

# headless 自动解题（脚本编排/批量刷题）
foxdog solve "一道 web 题，提示有备份文件泄露" \
    --target http://target:8080 \
    --flag-format 'flag\{.*\}' \
    --attach ./chal.zip \
    --yolo --max-minutes 30

# 运行中 steer 指挥：headless tty 下直接打字回车即注入（"先试 .git 泄露"）

# 单次解题 TUI：事件时间线 + 黑板面板 + steer 输入框
foxdog solve "..." --tui

# swarm 模式：race 快路径 + 多 worker 协作（黑板派活去重）
foxdog solve "..." --workers 4 --planner-model deepseek-v4-flash --max-minutes 45

# race 异构引擎（多端点+claude CLI）默认自动；--no-engines 强制单 solver
foxdog solve "..." --workers 4 --no-engines

# JSONL 事件流（stdout 干净可管道，事件协议见 foxdog/protocol/types.py）
foxdog solve "..." --json | jq .kind

# MCP server 与工具清单（调试）
foxdog mcp

# 预构建 Docker CTF 工具镜像
foxdog runtime-build

# 历史 run
foxdog runs
```

exit code：`0`=flag 验证通过，`2`=预算耗尽，`3`=异常。

## 工作产物

每题一个 run 目录：`.foxdog-runs/<run-id>/`

```
rollout.jsonl   # 首行 SessionMeta，后续 message/tool_call/tool_result/event/compaction（worker 记录带 worker 字段）
winner.json     # FlagGate 通过后写入（flag + 溯源原因）
writeup.md      # solve 结束自动生成的 writeup 骨架（成败都有）
blackboard.db   # SQLite 黑板（WAL）：events 事件溯源 + facts/intents/deadends 物化视图
inputs/         # --attach 附件
work/           # agent 工作区（exploit 脚本；文件工具限于此目录）
cas/<aa>/<sha256>   # 内容寻址存储：工具输出全文归档（截断输出的 fetch_output 取回、FlagGate 溯源证据、微压缩句柄）
```

## 目录结构

```
foxdog/
├── protocol/types.py    # Op / Event（core 只发事件，不知前端存在）
├── llm/                 # OpenAI 兼容流式客户端（重试/主备切换）、Anthropic 客户端（协议转换）、
│                        # 消息与 tool_call 重组、FakeProvider
├── executor/            # 子进程执行器（run_shell/run_argv，空闲/总超时、截断+CAS spill）、
│                        # local/docker/docker-persistent runtime（spawn 长进程）、后台任务、持久 shell
├── runtime_image/       # 无模型凭据、含固定 Claude Code CLI 的 Kali CTF 工具镜像
├── recipes/             # YAML 外部工具配方：loader（扫描+which 检测）、tool（RecipeTool/select_tools/recipe_help）、
│                        # builtin/*.yaml ×10
├── skills/              # 技能系统：loader（manifest+SKILL.md 扫描、requires.tools 检测）、tool（activate_skill）、
│                        # builtin/ 三个实战手册
├── swarm/             # 协作层：blackboard（SQLite 事件溯源黑板）、planner（ReasonPlanner 严格 JSON）、
│                        # coordinator（race+OODA 主循环）、worker（WorkerFactory 子 loop 工厂）、
│                        # facts（FactsBoard 门面：render 注入系统提示）
├── tools/builtin/       # shell_exec、shell_session、read/write/edit_file、glob、grep、python_eval、http_request、
│                        # submit_flag、todo_list、post_fact、post_deadend、list_board、claim_intent、complete_intent、
│                        # spawn_agent、wait_agent、list_agents、fetch_output、task_list/output/stop、
│                        # config_edit + copilot（/setting copilot 工具，物理隔离）
├── gate/flag_gate.py    # 反幻觉闸门：格式 + 非占位符 + 溯源三重校验
├── core/                # loop（turn/step + step 级预算 + 压缩钩子）、scheduler（资源冲突感知并发）、
│                        # orchestrator（权限→执行→CAS 归档）、permission（策略链）、goal（预算+自动续跑+steer 注入）、
│                        # budget（BudgetState/BudgetExceeded，None=无限）、compaction（微压缩+全量压缩）、
│                        # steer（中途指挥队列）、envprobe（环境事实探测）、context（CAS 取回）、prompts
├── mcp_client.py        # MCP client（stdio/HTTP，单 server 失败不致命，select_tools 渐进披露）
├── runner.py            # solve 装配线：build_run / execute_run（headless 与 TUI 共用；平台自动提交）
├── widgets.py           # TUI 组件：ToolCallRow（可折叠工具行）、黑板渲染器（分组/折叠/+N more）
├── tui.py               # Textual TUI（--tui）：时间线 + 黑板面板 + steer 输入框 + 状态行
├── tui_driver.py        # Windows CSI/ESC 原子拼接 + drag-only 鼠标协议，两套 TUI 共用
├── interactive.py       # 交互式会话 TUI（裸 foxdog / foxdog chat）：持续对话历史 + 每任务独立 run、
│                        # /model 引导配置、/setting copilot、命令补全、/fetch、/board、/budget、Esc、Ctrl+C 复制
├── store/               # workspace（run 目录+CAS）、rollout（JSONL 异步写）、writeup（writeup 骨架生成）
├── config.py            # 分层配置（含 model.planner、agent.context_window、mcp_servers、platform）+
│                        # ConfigStore 可写配置（原子落盘 0600）
└── cli.py               # typer 入口：裸命令/chat、solve / runs / mcp / runtime-build / kali
```

## 测试

```bash
.venv/Scripts/python -m pytest -q --basetemp=.pytest-tmp
```

当前基线：`221 passed`。除输入协议外，回归覆盖 Ctrl+C 安全复制/忙时中断/空闲双击退出、三行线框布局、活动状态避让、UTF-8/CP936 混合与跨分块解码、文件内容 10 行折叠、edit diff、常驻成功/失败色、Ctrl+B detach、后台完成通知/CAS 归档、DeepSeek Anthropic 多段 thinking/tool 往返、截断/过滤工具调用丢弃、空响应与强制工具契约有限恢复、turn 分片续跑、max-turns 预算，以及 stalled race 提前切换 swarm。
