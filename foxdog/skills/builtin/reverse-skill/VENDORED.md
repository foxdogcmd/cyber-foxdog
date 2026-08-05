# reverse-skill Vendor Note

- Source: https://github.com/zhaoxuya520/reverse-skill
- Vendored commit: `d8bf34540cbc1aa34052e1b142576fc36a1f1437`
- License: MIT for `reverse-skill`, preserved in `LICENSE`
- Included sub-bundle `CTF-Sandbox-Orchestrator/` is GPLv3 and keeps its own
  `CTF-Sandbox-Orchestrator/LICENSE`
- Included paths: `skills/`, `kali/`, `CTF-Sandbox-Orchestrator/`, README/RULES files

FoxDog loads every `SKILL.md` under `skills/` as a separate built-in skill while
keeping this directory as the resource root, so references such as
`../field-journal/...`, `../scripts/...`, and `../../CTF-Sandbox-Orchestrator/...`
remain resolvable through `activate_skill(name, resource=...)`.
