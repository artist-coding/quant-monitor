# 部署单元说明

> 换一台机器部署,先看 [NEW_SERVER.md](NEW_SERVER.md):那份讲的是 `git clone` 带不过来的东西
> (密钥、1.8G 行情库、单元里写死的 `/home/zx`、Node 版本、时区、linger),
> 这份讲的是单元本身怎么装、每条 timer 为什么排在那个点。

八份 systemd 单元。前四份是后端/前端服务,按机器和权限选一套,不要混装;
后四份是两条定时同步(日线、活跃市值),和上面哪一套都能共存。

| 文件 | 类型 | 跑在 | 端口 | 说明 |
| --- | --- | --- | --- | --- |
| `quant-monitor-api.service` | system | root | 8000 | 旧服务器,root 部署在 `/root/quant-monitor` |
| `quant-monitor-api.service.newserver` | system | zx | 8010 | 新机 system 级方案,需要 sudo |
| `quant-monitor-api.service.user` | **user** | zx | 8010 | 新机实际在用,不需要 sudo |
| `quant-monitor-web.service.user` | **user** | zx | 4173 | 前端生产构建,不需要 sudo |
| `quant-monitor-sync.service.user` | **user** | zx | — | 收盘后全市场日线同步(oneshot,由 timer 拉起) |
| `quant-monitor-sync.timer.user` | **user** | zx | — | 周一至周五 20:00 + 22:00 触发上面那条 |
| `quant-monitor-amv.service.user` | **user** | zx | — | 活跃市值同步(百度网盘 → `amv_daily`,oneshot) |
| `quant-monitor-amv.timer.user` | **user** | zx | — | 周二至周六 06:00 触发上面那条,一天一次 |

新机上 8000 端口被本机其他用户占用,所以后端一律走 8010。
`frontend/vite.config.ts` 里 dev(5173) 和 preview(4173) 两份代理都指向 8010。

## 用户级安装(当前本机采用)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/quant-monitor-api.service.user ~/.config/systemd/user/quant-monitor-api.service
cp deploy/quant-monitor-web.service.user ~/.config/systemd/user/quant-monitor-web.service

npm --prefix frontend install
npm --prefix frontend run build          # 前端服务托管 dist/,必须先构建

systemctl --user daemon-reload
systemctl --user enable --now quant-monitor-api quant-monitor-web
```

装完访问 <http://localhost:4173/>。

## 日线定时同步

```bash
cp deploy/quant-monitor-sync.service.user ~/.config/systemd/user/quant-monitor-sync.service
cp deploy/quant-monitor-sync.timer.user   ~/.config/systemd/user/quant-monitor-sync.timer

systemctl --user daemon-reload
systemctl --user enable --now quant-monitor-sync.timer   # 注意 enable 的是 timer,不是 service
```

跑的是 `scripts/sync_daily_kline.py`,它拿 `daily_kline` 的最新日期和本地
交易日历一比,把落下的交易日一并补上,而不只是同步当天。所以关机几天再开机
也能自己追上,不用手工回补。

```bash
systemctl --user list-timers quant-monitor-sync.timer   # 看下次触发
systemctl --user start quant-monitor-sync.service       # 立刻跑一次(幂等)
journalctl --user -u quant-monitor-sync.service -n 50   # 看日志
tail -f data/logs/sync_daily_kline.log                  # 同样的日志,落盘一份
```

手工执行也行:`python3 scripts/sync_daily_kline.py --date 20260825`。

### 为什么是两个触发点

20:00 主跑(北京时间;本机时区 `Asia/Shanghai`,`OnCalendar` 走本地时间。
A 股 15:00 收盘,Tushare 全市场 daily 通常 15:30~16:00 出全,20:00 早已出全),
22:00 兜底。`Type=oneshot` **不接受 `Restart=`**——写了 systemd 会拒绝加载
整个单元,所以重试不能靠 Restart,只能靠脚本内部的单日退避重试加这第二个触发点。
20:00 成功的话,22:00 那次查出没有待补交易日就秒退,不产生任何接口调用。

**改 `OnCalendar` 后 `daemon-reload` 会立刻补跑一次**:`Persistent=true` 记的是
上次触发时刻,新日历下若存在一个"已经过去但没跑过"的触发点,systemd 判定为错过、
马上补。这是预期行为(两条同步都幂等),但别以为是自己手滑点了 start。

节假日不需要在 timer 里排除:脚本查本地 `trade_cal`,非交易日直接 skipped。
但**交易日历要提前备好**——`trade_cal` 接口限流 **1 次/小时**(2026-09-01 实测),
现拉多半拉不到。每年底跑一次 `zt sync trade-cal --start 20270101 --end 20271231`。

## 活跃市值定时同步

活跃市值是选股的**总开关**(`modules/amv.py`)。它停更一天,那天就没有开关可用——
`get_regime` 会向前回退到最近一条,拿几天前的区间继续放行或拦截选股,
**而且不报任何错**。这条 timer 就是为了让它别停更。

数据来源是百度网盘的一个分享链接(用户每天更新)。百度网盘没有"直接下载分享文件"
的公开接口,只能走官方 App 的路子:先把分享文件**转存**到自己的网盘,再从自己的
网盘下载,最后删掉中转文件。驱动是 [BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go)。

```
分享链接 ──转存──> 自己的网盘 /auto_dl ──下载──> 临时目录
                        └──删除中转文件      └──> data/amv/baidu/活跃市值_YYYYMMDD.xlsx ──导入──> amv_daily
```

### 一次性准备

```bash
# 1. 装 BaiduPCS-Go(静态二进制,无依赖)。装到 ~/.local/bin 不需要 sudo,
#    脚本会依次找 /usr/local/bin、~/.local/bin、$PATH。
curl -fL -o /tmp/pcs.zip \
  https://github.com/qjfoidnh/BaiduPCS-Go/releases/download/v4.0.2/BaiduPCS-Go-v4.0.2-linux-amd64.zip
unzip -j -o /tmp/pcs.zip '*/BaiduPCS-Go' -d /tmp
install -m 0755 /tmp/BaiduPCS-Go ~/.local/bin/BaiduPCS-Go

# 2. 登录。必须用**完整 Cookie**,不能只给 BDUSS——转存强制需要 STOKEN,
#    只写 BDUSS 能通过 `who` 检查,但转存那步必失败。
#    浏览器登录 pan.baidu.com → F12 → Network → 刷新 → 第一条请求 →
#    Request Headers 里 Cookie: 后面一整串(要含 BDUSS= 和 STOKEN=)
#    Cookie 很长且含分号,别直接往命令行里贴(引号一漏就被 shell 拆开),用 read 读:
read -r -p "粘贴 Cookie 后回车: " C && BaiduPCS-Go login -cookies="$C" && unset C

# 2b. 验证。`who` 通过**不代表**能转存——只写了 BDUSS 的话 who 照样显示 uid,
#     但转存那步必失败。所以要单独确认 STOKEN 真的进去了。
#
#     注意别只看配置里的 `stoken` 字段:走 -cookies 登录时 BaiduPCS-Go
#     **不会**把 STOKEN 拆出来单独存,该字段永远是空的,STOKEN 留在 `cookies`
#     整串里。internal/pcsconfig/baidu.go 的 Baidu.BaiduPCS() 对这种情况有分支:
#         if strings.Contains(baidu.COOKIES, "STOKEN=") && baidu.STOKEN == ""
#     所以两种状态都算正常,只有两处都没有才是真的缺。
BaiduPCS-Go who        # 出现「当前帐号 uid: 非0数字」
python3 -c "
import json, pathlib
c = json.loads((pathlib.Path.home()/'.config/BaiduPCS-Go/pcs_config.json').read_text())
for u in c.get('baidu_user_list') or []:
    ck = u.get('cookies') or ''
    ok = bool(u.get('stoken')) or 'STOKEN=' in ck
    print(f\"uid={u['uid']} bduss={'有' if u.get('bduss') else '缺'} \"
          f\"stoken字段={'有' if u.get('stoken') else '空'} \"
          f\"cookies含STOKEN={'是' if 'STOKEN=' in ck else '否'} \"
          f\"→ 转存{'可用' if ok else '不可用'}\")
"
chmod 600 ~/.config/BaiduPCS-Go/pcs_config.json   # Cookie 是明文存的

# 3. 配分享链接。**不能写进脚本**,主仓库是公开的。
#    往 .env 里加 AMV_BAIDU_SHARE_URL / AMV_BAIDU_SHARE_PWD,其余可选项见 .env.example。
```

Cookie 大概 **1~3 个月**会过期,过期后重做第 2 步。配了
`AMV_BAIDU_NOTIFY_CMD` 的话脚本会告警,否则只能靠 journalctl。

### 装 timer

```bash
cp deploy/quant-monitor-amv.service.user ~/.config/systemd/user/quant-monitor-amv.service
cp deploy/quant-monitor-amv.timer.user   ~/.config/systemd/user/quant-monitor-amv.timer

systemctl --user daemon-reload
systemctl --user enable --now quant-monitor-amv.timer   # enable 的是 timer,不是 service
```

```bash
systemctl --user start quant-monitor-amv.service     # 立刻跑一次(整表 upsert,幂等)
journalctl --user -u quant-monitor-amv.service -n 50
tail -f data/logs/sync_amv.log                       # 入库侧日志
tail -f data/logs/sync_amv_baidu-$(date +%Y%m).log   # 下载侧日志,按月分文件
```

手工执行的几种用法:

```bash
python3 scripts/sync_amv.py                    # 下载 + 入库(timer 跑的就是这条)
python3 scripts/sync_amv.py --skip-download    # 不下载,把已归档的最新文件重新入库
python3 scripts/sync_amv.py --file x.zip --dry-run   # 只解析不落库,核对列名映射
zt amv import 某个文件 --dry-run                # 同上,CLI 版
zt amv status                                   # 看当前区间
```

导入器认 **csv / xlsx / zip** 三种容器,列名中英文都认(`date`/`日期`、
`close`/`收盘`/`收盘价` …),zip 会把里面所有表格拼起来。整表 upsert,
所以每天下全量表重复导入是幂等的。上游换了导出格式时先跑 `--dry-run`,
它会把读到的表头打出来。

### 为什么排在次日早上 06:00

**上游出数没准点**,当天收盘后未必拿得到当天的数据。2026-08-31 那天
16:09 / 20:05 / 22:05 三次下到的是同一份文件,末行都停在 08-28,第二天早上才
补上。活跃市值是选股总开关,只要 **09:30 开盘前**到位就不耽误当天决策;
排在收盘后那几个点,除了多转存一次什么也换不来。这条 timer 原先排 20:05 + 22:05,
2026-09-01 改成只留次日早上这一次。

`Tue..Sat` = 每个交易日的次日早晨:周一到周五收盘的数据分别由周二到周六早上
这次接。周一早上不排——上周五的数据周六那次已经接过了。

06:00 跑在当天日线同步(20:00)**之前**,但脚本那道"活跃市值落后几个交易日"的
检查照样成立:此刻库里日线最新是昨天,活跃市值也该是昨天,对得上就是 0,
对不上就是上游真落后了。(这道防线怕的是两边**一样旧**——所以千万别把它挪到
当天日线入库之前的几分钟内,那时两边都停在昨天以前,落后天数恒为 0,
不报错,只是不再报警。隔了一整夜没这个问题。)

**一天只转存一次,别再加触发点**:BaiduPCS-Go 是非官方客户端,频率高了会触发
百度的人机验证。代价是当天没有重试——下载失败就等下一个早上;上游是全量表、
整表 upsert,下一次跑会把中间断掉的几天一起补回来,`Persistent=true` 也会在
开机后补跑错过的触发点。

`quant-monitor-amv.service` 里保留了 `After=quant-monitor-sync.service`:两个 job
万一排进同一个事务(比如关机错过后开机一起补跑),systemd 会按序跑,免得两条同步
同时写同一个 SQLite 主库撞 `database is locked`。刻意不用 `Requires=`——
那会让每次活跃市值同步都顺带拉一次日线同步。

### 退出码

| 码 | 含义 | systemd |
| --- | --- | --- |
| 0 | 活跃市值已跟上行情库 | success |
| 2 | 入库成功,但落后 1~3 个交易日(上游当天还没出) | 单元里 `SuccessExitStatus=2`,算 success,日志有 WARNING |
| 1 | 下载失败、入库失败,或落后**超过 3 个交易日** | failed |

落后超过 3 个交易日为什么要报失败:那时候链接还能转存、文件也还在,只是上游
不再更新。这种坏法不报错的话没有任何征兆——`get_regime` 会一直拿几周前的
区间当今天用,该停手的时候照样放行选股。

## 六个会静默坏掉的点

1. **linger**:用户级服务在所有登录会话断开后会被停掉,且开机不自启。
   必须执行一次 `sudo loginctl enable-linger $USER`,
   用 `loginctl show-user $USER -p Linger` 确认是 `Linger=yes`。
   **对 timer 尤其致命**:没开 linger 时 `list-timers` 照常显示下次触发时间,
   看着一切正常,但你一登出用户实例就被杀,到点根本不会跑,也没有任何报错。

2. **前端改完要重新 build**:服务托管的是 `dist/` 里的产物,
   只 `systemctl --user restart quant-monitor-web` 不会重新编译。

3. **`vite preview` 不继承 `server.proxy`**:漏配 `preview.proxy` 的话
   页面照常打开、但所有 `/api` 请求 404,看着像后端挂了。

4. **代理**:本机 socks5 代理会打断 Tushare 通路,报错还指向错误方向
   (表现为连接超时,看着像对端挂了)。同步单元里用 `UnsetEnvironment=` 清掉,
   `scripts/sync_daily_kline.py` 顶部 import 之前又清了一遍,两处都别删。
   活跃市值那条同理:百度网盘是境内服务,走代理有害无益,
   `scripts/sync_amv_baidu.sh` 也会清(要保留就设 `AMV_BAIDU_KEEP_PROXY=1`)。

5. **BaiduPCS-Go 失败时退出码仍是 0**。`who` 未登录会正常打印
   「当前帐号 uid: 0」然后退 0;`transfer` 失败会打印
   「分享链接转存到网盘失败: …」然后也退 0。所以
   `scripts/sync_amv_baidu.sh` 全靠 grep 输出文本判断成败,
   不要"顺手"改成看退出码。

6. **网盘中转目录 `/auto_dl` 每次运行会被清空**,别往里放别的东西。
   不清的话下次转存会因同名文件失败。脚本默认还会顺带清空网盘回收站
   (中转文件删掉后会堆在那里占空间);回收站里有别的重要东西就设
   `AMV_BAIDU_CLEAR_RECYCLE=0`。

## EnvironmentFile 为什么不带 `-`

单元里刻意写成 `EnvironmentFile=/home/zx/quant-monitor/.env` 而不是 `=-`。
带 `-` 时配置文件缺失会被静默忽略,服务照常 running 但
`TUSHARE_TOKEN` / `LLM_API_KEY` 全空,同步与 LLM 静默失效。
