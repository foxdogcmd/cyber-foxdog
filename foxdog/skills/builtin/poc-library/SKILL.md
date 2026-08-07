# 个人 POC 库接入约定

FoxDog 内置的是按漏洞类型组织的知识、playbook、payload 和技能入口，不是全量
CVE/POC 数据库。新 POC 应该优先放进用户级 skill，这样不会随内置技能包更新被
覆盖，也能被 `activate_skill` 按需加载。

## 最便捷路径：一个 POC 一个用户 skill

准备目录：

```text
~/.config/foxdog/skills/<poc-id>/
├── manifest.yaml
├── SKILL.md
├── poc.py
└── references/
```

`manifest.yaml`：

```yaml
name: poc-cve-2024-xxxx
description: 一句话说明目标组件、漏洞类型、利用条件和影响版本
when: 遇到目标组件 <组件名> 且疑似 <漏洞类型> 时
requires:
  tools:
    - python3
```

`SKILL.md` 至少要包含：适用组件/版本、触发条件、POC 执行方式、成功判定、
常见失败原因。POC 脚本放在同目录，正文里用相对路径引用，例如：

```bash
python3 poc.py http://target:port/path
```

新任务启动时会自动发现该技能。也可以直接进 `/setting` 让配置 agent 调用
`manage_skill`：

```text
把 C:\Users\FoxxD\pocs\CVE-2024-xxxx 加为技能，name 叫 poc-cve-2024-xxxx
```

`manage_skill` 支持本地目录和 GitHub HTTPS 地址，并会保留 `SKILL.md` 同目录下的
POC 脚本和 references 资源。

## 其它 POC 形态

- 如果 POC 是独立 CLI 工具，加一份用户 recipe：
  `~/.config/foxdog/recipes/<poc-id>.yaml`，模型用 `select_tools` 激活。
- 如果是 nuclei 模板，放进当前 run 可访问的目录（例如 `inputs/nuclei-templates/`），
  再用 nuclei recipe 的 `templates=` 指向它。
- 如果是纯 payload/字典，放用户 payload 目录，不要改 vendored 内置包。

## 项目级贡献

想直接把 POC 合入 foxdog 仓库时，放在：

```text
foxdog/skills/builtin/pocs/<poc-id>/
├── manifest.yaml
├── SKILL.md
├── poc.py
├── LICENSE
└── VENDORED.md
```

`VENDORED.md` 记录来源仓库、commit、license，避免以后无法追溯和更新。
