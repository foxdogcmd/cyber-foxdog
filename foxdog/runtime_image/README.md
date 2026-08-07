# FoxDog tool runtime

Agent-facing runtime guide: [AGENTS.md](./AGENTS.md)

This image contains command-line CTF tooling plus a pinned Claude Code CLI.
FoxDog keeps the TUI, model clients, credentials, CAS, flag gate, and team
blackboard on the host. Claude credentials are injected per process at runtime;
they are never baked into an image layer.

FoxDog 只维护一套正式工具镜像：`Dockerfile.full` 构建并标记为
`foxdog-runtime:full`。配置和自动构建始终使用这个标签。旧的 `:latest`
精选/headless 变体已停用。

```text
# 推荐：FoxDog CLI 包装
foxdog runtime-build

# 等价的原生 Docker 命令
docker build -f foxdog/runtime_image/Dockerfile.full -t foxdog-runtime:full foxdog/runtime_image
```

## 工具内容

`Dockerfile.full` 不用 `kali-linux-headless` 元包，改用品类明确、版本可控的清单，避免元包拖进 AD/云/企业内网套件（netexec/responder/bloodhound 等跑题面）。当前镜像约 22.9GB，后续新增包会随重建增长。**国内构建**：apt 源默认按 `aliyun → tuna → http.kali.org`（HTTP）顺序回退——ghidra 504MB / metasploit 270MB 大文件走 http.kali.org 重定向器实测反复断连，国内镜像稳定。

- **Web 扫描/枚举**（apt）：feroxbuster、whatweb、wafw00f、arjun、wpscan、nuclei、nmap、masscan、ffuf、gobuster、sqlmap、hydra、ncrack、nikto
- **字典/密码**（apt）：seclists、john、hashcat
- **URL/JS/资产**：katana、dalfox（GitHub release 二进制）、gau、waybackurls（go install，GOPATH=/opt/go）、nuclei-templates（clone 到 `/opt/nuclei-templates`）
- **Misc/取证**（apt）：binwalk、exiftool、steghide、stegseek、stegcracker、stegsnow、foremost、tshark、tcpdump、sleuthkit、pngcheck、zbar-tools、testdisk、pcapfix、ffmpeg、sox、imagemagick、gifsicle、apngdis、multimon-ng、qrencode、sqlite3、xxd、hexedit；volatility3（venv pip，`vol`）
- **Pwn/逆向/APK**（apt + release + gem + venv）：radare2、patchelf、jadx、apktool、android-tools-adb、gdb+gef、checksec、strace、ltrace、seccomp-tools、upx、qemu-user；pwninit（release 裸 ELF）；zsteg、one_gadget（gem）；ropper、ROPgadget（venv）
- **网络/代理**（apt）：proxychains4、whois、sshpass、socat
- **全家桶**：metasploit-framework、ghidra、maxima-sage，以及 conda-forge 的 SageMath 10.9（`sage`）
- **Python exploit 栈**（`/opt/foxdog-venv`，PATH 置顶，与 Kali deb 版隔离）：pwntools、pycryptodome、sympy、gmpy2、z3-solver、capstone、angr、unicorn、r2pipe、frida-tools、yara-python、ropper、ROPgadget、volatility3、semgrep、objection、flask-unsign、pefile、oletools、lief、scapy、dnspython、dnslib、Pillow、numpy、matplotlib、qiling、fpylll、py_ecc、pyjwt、cryptography

Sage 安装在 `/opt/conda`。Miniforge 安装包固定版本和 SHA256，并通过 BuildKit cache mount + `curl --continue-at -` 断点续传；conda 包走清华 conda-forge 镜像并缓存到 `/opt/conda/pkgs`，网络失败后重建可复用已下载内容。

PATH 顺序：`/opt/foxdog-venv/bin:/opt/go/bin:/usr/local/bin:...`。注意 `bash -lc` 登录 shell 会重置 PATH 导致 venv/go 工具看似 MISSING——foxdog 的 `docker exec` 走非 login shell，继承镜像 ENV，正常。

镜像还把 Muteki 的静态 README/docs/skills/评测报告按固定 commit 抓取到
`/opt/foxdog/knowledge/muteki`。这只是容器内离线资料库，不注入默认 prompt，
也不进入黑板 facts；实际题目结论仍必须来自当前 run 的真实工具输出。

When `[runtime].kind` is `"docker"` / `"docker-persistent"` and the configured
image is absent, `runtime.auto_build = true` builds this full image automatically.

## docker-persistent（常驻容器）

`[runtime].kind = "docker-persistent"` 时，所有 solve run 共享常驻容器
`foxdog-kali`（绑定 `.foxdog-runs/` 根目录到 `/workspace`，每 run 独立
`/workspace/<run-id>/work`）。生命周期管理：

```text
foxdog kali start   # 创建/复用常驻容器（镜像缺失按 auto_build 构建）
foxdog kali status  # 状态/镜像/挂载
foxdog kali exec    # 无参数进 bash；跟参数跑单条命令
foxdog kali stop    # stop + rm
foxdog kali reset   # rm 后重建（清空容器内状态）
```
