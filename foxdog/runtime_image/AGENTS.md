# FoxDog Container Agent Guide

这是给 FoxDog 解题 agent / worker 的容器使用说明。它描述的是命令执行环境，
不是 FoxDog 主程序的部署方式。

## 执行边界

- FoxDog 主 agent、TUI、模型客户端、模型凭据、CAS、FlagGate 和黑板在宿主机。
- `shell_exec`、`python_eval`、`shell_session`、外部工具配方和容器化 Claude race 在 Kali Linux 容器里执行。
- 所有 worker 共享同一个 solve run 的执行环境和工作区；不要假设只有自己在改文件或打靶机。
- 容器用户是 `foxdog`，有 NOPASSWD sudo。`apt`、`apt-get`、`dpkg` 有自动 sudo 包装；其他需要提权的命令显式加 `sudo -n`。
- 镜像里不烘模型 token、本机 home、历史 session 或 Docker socket。Claude Code 凭据只在启动 Claude race 时按进程注入。

## 路径和工作区

- 当前目录是本题工作目录。临时容器通常是 `/workspace/work`；常驻容器是 `/workspace/<run-id>/work`。
- `/workspace` 是宿主 run 目录的 bind mount。容器里写入 `/workspace/...`，宿主 run 目录也能看到。
- 文件工具和 shell 看见的是同一批文件：`read_file` / `write_file` / `edit_file` 只能读写 `/workspace` 工作区。
- 宿主机桌面、下载目录、题目附件等不在工作区时，先用 `import_file` 复制进 `inputs/`，再使用它返回的 workspace 相对路径或容器绝对路径。
- 不要猜 Windows 路径和容器路径的对应关系；不要让文件工具读写 `/workspace` 之外。容器内 `/tmp` 适合临时命令产物，但需要留给后续分析的文件应放回 `/workspace/work`。
- `resume` 会把旧 run 的 `work/`、`inputs/`、`cas/` 和 `blackboard.db` 复制到新 run，续跑时优先复用已有脚本、扫描结果和事实。

## 工具可用性

- 先用 `which <tool>` 或 `<tool> --help` 确认可用性，不要凭记忆断言缺工具。
- 常见 Web 工具：`nmap`、`ffuf`、`feroxbuster`、`gobuster`、`whatweb`、`wafw00f`、`arjun`、`wpscan`、`nuclei`、`nikto`、`sqlmap`、`hydra`、`katana`、`gau`、`waybackurls`、`dalfox`。
- 常见 Pwn / Rev 工具：`checksec`、`gdb` + GEF、`radare2`、`ROPgadget`、`ropper`、`one_gadget`、`pwninit`、`patchelf`、`strace`、`ltrace`、`ghidra`。
- 常见 Crypto / Python 栈：`python3`、`pwntools`、`pycryptodome`、`sympy`、`gmpy2`、`z3`、`angr`、`unicorn`、`r2pipe`、`sage`。
- 常见 Misc / Forensics 工具：`binwalk`、`exiftool`、`steghide`、`foremost`、`tshark`、`tcpdump`、`sleuthkit`、`pngcheck`、`zbarimg`、`vol`。
- Nuclei templates 在 `/opt/nuclei-templates` 和 `/usr/local/share/nuclei-templates`。
- PATH 已把 `/opt/foxdog-venv/bin` 和 `/opt/go/bin` 放前面。不要用 `bash -lc` 做工具探测；login shell 可能重置 PATH。

## 工作方式

- 开工先看题面、目标、附件和黑板，再选方向。确认新事实用 `post_fact`；确认死路用 `post_deadend`。
- facts 只放可验证事实；操作员/旁观者纠偏属于控制指令，不要把它当成靶机事实。
- 长扫描或监听器用 `shell_exec(background=true)`，交互式 `nc`、`gdb`、`ssh`、`ftp` 用 `shell_session`。
- 大输出会进入 CAS 并返回 handle；需要全文时用 `fetch_output` 分段取，不要重复跑昂贵扫描。
- 多 worker 会并发操作共享靶机。写文件、覆盖配置、跑爆破前先看黑板里的已排除方向和控制指令，避免重复或互相踩踏。
- 写 exploit 脚本、payload、扫描结果放在当前工作目录，并用有意义的文件名，例如 `exploit.py`、`scan-nmap.txt`、`notes.md`。

## Flag 纪律

- 只提交目标真实产出的 flag 原文。不要改写前缀、大小写或正文。
- 不要伪造 flag 样字符串写入本地或目标后再提交；FlagGate 会检查工具输入和输出时序。
- crypto / rev 计算出的 flag 先让它出现在工具输出里，再调用 `submit_flag`。
- 候选 flag 必须用 `submit_flag` 过闸；只在自然语言回复里说拿到了不算完成。
