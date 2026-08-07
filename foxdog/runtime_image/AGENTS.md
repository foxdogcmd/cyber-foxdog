# FoxDog Container Agent Guide

这是给 FoxDog 解题 agent / worker 的容器使用说明。它描述的是命令执行环境，
不是 FoxDog 主程序的部署方式。

## 执行边界

- FoxDog 主 agent、TUI、模型客户端、模型凭据、CAS、FlagGate 和黑板在宿主机。
- `shell_exec`、`python_eval`、`shell_session`、外部工具配方和容器化 Claude race 在 Kali Linux 容器里执行。
- 所有 worker 共享同一个 solve run 的执行环境、公共 run 工作区、黑板和 FlagGate；不要假设只有自己在改公共文件或打靶机。
- 容器用户是 `foxdog`，有 NOPASSWD sudo。`apt`、`apt-get`、`dpkg` 有自动 sudo 包装；其他需要提权的命令显式加 `sudo -n`。
- 镜像里不烘模型 token、本机 home、历史 session 或 Docker socket。Claude Code 凭据只在启动 Claude race 时按进程注入。

## 路径和工作区

- 主 solver 当前目录是本题公共工作目录。临时容器通常是 `/workspace/work`；常驻容器是 `/workspace/<run-id>/work`。
- swarm/子 agent worker 有自己的私有 cwd，位于公共 work 下的 `.workers/<worker-id>/`。`shell_exec`、`python_eval`、`shell_session`、文件工具和外部配方的相对路径默认落在这个私有目录。
- swarm/子 agent worker 的 `TMPDIR`/`TMP`/`TEMP` 也指向私有 cwd 下的 `.tmp`。临时文件优先用 `$TMPDIR` 或 `mktemp`，不要硬编码 `/tmp/foo` 这种全队共享路径。
- 需要给其他 worker 或后续 resume 共享的文件，显式写入公共 work 目录、`inputs/`，或使用系统提示给出的 `/workspace/...` 绝对路径。
- `/workspace` 是宿主 run 目录的 bind mount。容器里写入 `/workspace/...`，宿主 run 目录也能看到。
- 文件工具和 shell 看见的是同一批文件：`read_file` / `write_file` / `edit_file` 只能读写 `/workspace` 工作区。
- 宿主机桌面、下载目录、题目附件等不在工作区时，先用 `import_file` 复制进 `inputs/`，再使用它返回的 workspace 相对路径或容器绝对路径。
- 不要猜 Windows 路径和容器路径的对应关系；不要让文件工具读写 `/workspace` 之外。容器内 `/tmp` 适合临时命令产物，但需要留给后续分析的文件应放回公共 work 区或自己的 worker cwd。
- `resume` 会把旧 run 的 `work/`、`inputs/`、`cas/` 和 `blackboard.db` 复制到新 run，续跑时优先复用已有脚本、扫描结果和事实。

## 工具可用性

- 先用 `which <tool>` 或 `<tool> --help` 确认可用性，不要凭记忆断言缺工具。
- 常见 Web 工具：`nmap`、`masscan`、`ffuf`、`feroxbuster`、`gobuster`、`whatweb`、`wafw00f`、`arjun`、`wpscan`、`nuclei`、`nikto`、`sqlmap`、`hydra`、`ncrack`、`katana`、`gau`、`waybackurls`、`dalfox`。
- 常见 Pwn / Rev 工具：`checksec`、`gdb` + GEF、`radare2`、`ROPgadget`、`ropper`、`one_gadget`、`pwninit`、`patchelf`、`strace`、`ltrace`、`seccomp-tools`、`upx`、`qemu-user`、`ghidra`、`jadx`、`apktool`、`adb`。
- 常见 Crypto / Python 栈：`python3`、`pwntools`、`pycryptodome`、`sympy`、`gmpy2`、`z3`、`angr`、`unicorn`、`r2pipe`、`sage`。
- 常见 Misc / Forensics 工具：`binwalk`、`exiftool`、`steghide`、`stegseek`、`stegcracker`、`stegsnow`、`foremost`、`tshark`、`tcpdump`、`sleuthkit`、`pngcheck`、`zbarimg`、`testdisk`、`pcapfix`、`ffmpeg`、`sox`、`convert`、`gifsicle`、`apngdis`、`multimon-ng`、`qrencode`、`sqlite3`、`xxd`、`hexedit`、`vol`。
- 网络 / 代理：`proxychains4`、`whois`、`sshpass`、`socat`。
- Python 栈额外可用：`semgrep`、`objection`、`flask-unsign`、`pefile`、`oletools`、`lief`、`scapy`、`dnspython`、`dnslib`、`Pillow`、`numpy`、`matplotlib`、`qiling`、`fpylll`、`py_ecc`、`pyjwt`、`cryptography`。
- Nuclei templates 在 `/opt/nuclei-templates` 和 `/usr/local/share/nuclei-templates`。
- PATH 已把 `/opt/foxdog-venv/bin` 和 `/opt/go/bin` 放前面。不要用 `bash -lc` 做工具探测；login shell 可能重置 PATH。

## 离线资料

- Muteki 静态知识库在 `/opt/foxdog/knowledge/muteki`，包含 README、AGENTS、docs、skills 和评测报告摘要，只作离线参考。
- 资料库内容是线索，不是靶机事实；不要直接写入 facts。只有从当前题目真实输出中验证过的结论才进黑板 facts。
- 资料库不包含 Muteki 运行状态、历史 session、黑板 DB、模型凭据或宿主 home。

## 工作方式

- 开工先看题面、目标、附件和黑板，再选方向。确认新事实用 `post_fact`；确认死路用 `post_deadend`。
- facts 只放可验证事实；操作员/旁观者纠偏属于控制指令，不要把它当成靶机事实。
- 长扫描或监听器用 `shell_exec(background=true)`，交互式 `nc`、`gdb`、`ssh`、`ftp` 用 `shell_session`。
- 大输出会进入 CAS 并返回 handle；需要全文时用 `fetch_output` 分段取，不要重复跑昂贵扫描。
- 多 worker 会并发操作共享靶机。写公共文件、覆盖配置、跑爆破前先看黑板里的已排除方向和控制指令，避免重复或互相踩踏。
- 写本 worker 的 exploit 脚本、payload、扫描结果默认放在当前私有目录，并用有意义的文件名，例如 `exploit.py`、`scan-nmap.txt`、`notes.md`；确定要共享时再复制到公共 work 区。

## Flag 纪律

- 只提交目标真实产出的 flag 原文。不要改写前缀、大小写或正文。
- 不要伪造 flag 样字符串写入本地或目标后再提交；FlagGate 会检查工具输入和输出时序。
- crypto / rev 计算出的 flag 先让它出现在工具输出里，再调用 `submit_flag`。
- 候选 flag 必须用 `submit_flag` 过闸；只在自然语言回复里说拿到了不算完成。
