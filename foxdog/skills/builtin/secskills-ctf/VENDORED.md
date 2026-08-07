# secskills-ctf Vendor Note

- Source: https://github.com/ljagiello/ctf-skills
- Vendored commit: `d6662d26b5ed3caa56f5eaf6eb887964f3747162`
- License: MIT, preserved in `LICENSE`
- Included paths: `skills/` (ctf-web/crypto/pwn/reverse/forensics/misc/osint/malware/ai-ml/writeup/solve-challenge), `scripts/`, `README.md`

FoxDog treats this directory as a bundle resource root. Every `SKILL.md`
under `skills/` is loaded as a separate skill, while references such as
`../scripts/...` resolve against the bundle root.
