# Web 题侦察方法论（开局 10 分钟该做的事）

原则：**先捡现成的，再上爆破**。CTF Web 题 60% 的入口在源码泄露和注释里，
不在字典爆破里。每发现一条都 post_fact，排除的面都 post_deadend。

## 1. 指纹与第一眼看的东西

- `http_request` GET `/`，读：响应头（Server/X-Powered-By/Set-Cookie 的
  框架特征，如 `PHPSESSID`=PHP、`JSESSIONID`=Java）、HTML 注释
  （`<!-- -->` 里常直接放提示甚至 flag）、`<script src>` 路径风格。
- robots.txt / sitemap.xml / security.txt —— 常藏隐藏路径。
- favicon 哈希认框架（可选）；错误页认中间件（触发 404/500 看堆栈）。
- 重定向链：用 follow_redirects=false 看 301/302 的 Location。

## 2. 源码泄露（命中率最高，逐个试）

按概率从高到低（全部用 http_request 或直接写测绘脚本批量打）：

- `/.git/HEAD` → 200 且含 `ref:` 即 .git 泄露。接下来：
  `/.git/config`（看 remote）、`/.git/index`，然后用 git-dumper 类脚本
  （python_eval 里写，或 shell 里 curl 拉 objects）重建整个仓库，
  `git log` / `git show` 里翻 flag 和凭据。**这是 CTF 第一大送分点**。
- `/.svn/entries`、`/.svn/wc.db` → svn 泄露，思路同上。
- `/.DS_Store`（mac 垃圾文件，泄露目录结构，可用 python 解析）。
- `/.hg/`、`/.bzr/`（少见但顺手一打）。
- `composer.json` / `package.json` / `requirements.txt` / `pom.xml`
  （技术栈与依赖版本，可能直接指向已知漏洞组件）。

## 3. 备份文件 fuzz（针对已发现的每个页面）

发现 `index.php`、`login.php`、`upload.php` 后，对每个名字批量试：

```
index.php~  index.php.bak  index.php.swp  .index.php.swp
index.php.swo  index.bak  index.php.old  index.php.orig
index.php.txt  index.php1  index.php.bakup  index.php%20
index.php/.  index.php%00  #index.php#（vim 崩溃恢复文件）
```

`.swp` 是 vim 崩溃残留，`vim -r` 可恢复源码；`.bak/.old/.save` 同理。
响应 200 且 content-type 不是 text/html 的尤其要细看。
写个 20 行的 python 测绘脚本跑，别手点。

## 4. 目录与文件爆破

确认过手动途径没货再爆破，**先把字典挑对**（技术栈决定扩展名）：

- 有 select_tools：先 `select_tools()` 看有没有 dirsearch/ffuf/gobuster，
  激活后用配方跑（后台跑长任务：`select_tools` 激活 → 调用配方工具）。
- 字典顺序：小（common.txt 千级）→ 中（raft-medium 万级）→ 大。
  扩展名单子：按指纹给 `php,html,bak,txt,zip,sql,conf,json,log`。
- 递归深度 ≤2，状态码噪音用 `-fc 404`/按响应大小过滤。
- 爆破是长任务：shell_exec background=true 或配方 timeout，期间继续手工分析。

常用手工路径（不用字典也值得直接试）：
`/flag`、`/flag.php`、`/flag.txt`、`/admin`、`/admin.php`、`/login`、
`/upload`、`/uploads/`（传过的文件回显）、`/backup.zip`、`/www.zip`、
`/site.zip`、`/db.sql`、`/dump.sql`、`/phpinfo.php`、`/info.php`、
`/test.php`、`/api`、`/console`（flask debug）、`/actuator`（spring）、
`/server-status`（apache）。

## 5. 常见 flag 位置清单（拿到任何执行/读取原语后按此搜）

- Web 根：`/flag`、`/flag.php`、`/flag.txt`、`/flag_is_here/`。
- 系统根：`/flag`（Linux 容器题惯例）、`/flag.txt`、`/readflag`
  （suid 二进制，要 getshell 后执行）。
- 环境变量：`env` / `/proc/1/environ`（docker 题 flag 常在 ENV 里）。
- 数据库：库名/表名含 flag（`flag` 表 `flag` 列）；先 `show databases`。
- 配置文件：`config.php`、`wp-config.php`、`.env`（Laravel/Node）、
  `application.properties`（Java）。
- PHP 题特有：`phpinfo()` 页搜 flag；session 文件 `/tmp/sess_*`。
- 源码注释、JS 文件注释、CSS 注释、HTTP 响应头自定义字段
  （`X-Flag:` 之类，拿 curl -i 全量看头）。

## 6. 侦察收尾

- 技术栈、开放路径、泄露文件、可疑参数全部 post_fact。
- 没有发现 = 重要结论，post_deadend（如"无 .git/.svn/备份泄露"），
  然后带着事实板进漏洞利用阶段（注入 → activate_skill("sqli-basics")，
  文件写入 → activate_skill("php-file-write-waf")）。
