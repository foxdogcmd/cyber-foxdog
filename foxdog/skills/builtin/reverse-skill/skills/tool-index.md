# FoxDog Runtime Tool Index

This is a FoxDog-specific static index for the vendored `reverse-skill` bundle.
It exists because upstream keeps `tool-index.md` machine-generated and only
tracks `tool-index.md.template`. Before relying on any binary, still confirm it
inside the active runtime with `which <tool>` or `<tool> --help`.

| Tool | Skill | Purpose | Expected in FoxDog runtime |
|------|-------|---------|----------------------------|
| `jadx` | `apk-reverse` | APK/Dex Java decompilation | yes after rebuilding `foxdog-runtime:full` |
| `apktool` | `apk-reverse` | APK resource/smali decode and rebuild | yes after rebuilding `foxdog-runtime:full` |
| `adb` | `apk-reverse` / `mobile-reverse` | Android device bridge | yes after rebuilding `foxdog-runtime:full` |
| `frida`, `frida-ps` | `apk-reverse` / `mobile-reverse` | Dynamic instrumentation | yes via `/opt/foxdog-venv` |
| `ghidra` | `ghidra-reverse` / `reverse-engineering` | GUI/headless reverse engineering | yes |
| `radare2`, `rabin2`, `r2pipe` | `radare2` / `reverse-engineering` | CLI reverse engineering | yes |
| `gdb`, `gef` | `pwn-chain` / `reverse-engineering` | Native debugging | yes |
| `checksec`, `ROPgadget`, `ropper`, `one_gadget`, `pwninit`, `patchelf` | `pwn-chain` | ELF exploitation helpers | yes |
| `angr`, `unicorn`, `capstone`, `z3` | `reverse-engineering` / `pwn-chain` | Symbolic execution and emulation | yes via `/opt/foxdog-venv` |
| `binwalk`, `exiftool`, `tshark`, `tcpdump`, `sleuthkit`, `vol` | `digital-forensics` / `firmware-pentest` | Forensics and firmware triage | yes |
| `nmap`, `ffuf`, `feroxbuster`, `gobuster`, `sqlmap`, `hydra`, `nikto` | `pentest-tools` | Authorized recon and exploitation | yes |

PATH in the container starts with `/opt/foxdog-venv/bin:/opt/go/bin:/usr/local/bin`.
Do not use `bash -lc` for tool discovery because login shell startup can reset PATH.
