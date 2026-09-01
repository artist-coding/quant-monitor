# 换一台服务器要做什么

`deploy/README.md` 讲的是八份 systemd 单元本身：怎么装、每条 timer 为什么排在那个点、
哪些地方会静默坏掉。这份讲的是**换机器**：`git clone` 带不过来、单元文件里也写不进去的那些东西。

两份配合着看——下面凡是提到"照 deploy/README.md 装"的地方，都不再重复那边的内容。

## git clone 带不过来的六样东西

| 东西 | 在哪 | 为什么带不走 |
| --- | --- | --- |
| `.env`（Tushare Token、LLM Key、网盘分享链接） | 仓库根 | `.gitignore` 排除。主仓库是 public，密钥进去就等于公开 |
| `data/stock_data.db` | `data/` | 1.8G，`daily_kline` 1075 万行。体积超 GitHub 上限，且是 Tushare 行情数据，转发受其条款约束 |
| Python 依赖 | `~/.local/lib/python3.10/site-packages` | 要在新机重装，且**装到哪里决定了单元文件怎么写**（见下） |
| `frontend/node_modules` + `frontend/dist` | `frontend/` | 必须在目标机重装重建 |
| BaiduPCS-Go 二进制 + Cookie | `~/.local/bin/`、`~/.config/BaiduPCS-Go/` | Cookie 明文存储且 1~3 个月过期 |
| 单元里的绝对路径、linger | `deploy/*.user`、`loginctl` | 八份单元全部硬编码 `/home/zx`；linger 是机器级开关 |

## 0. 系统前置

```bash
sudo apt update
sudo apt install -y git curl unzip zstd python3-pip
```

`unzip` 用来装 BaiduPCS-Go，`zstd` 用来解备份快照——这两个都是到了用的时候才发现没装。

**Node 版本是硬门槛**：`frontend` 用的 vite 8，`engines` 写死 `^20.19.0 || >=22.12.0`。
Ubuntu 22.04 的 `apt install nodejs` 装的是 12.22，跑 `npm run build` 会直接报 engine 不匹配。
走 NodeSource 或 nvm：

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node -v          # 要 >= 22.12
```

Python 要 ≥ 3.10（`pyproject.toml` 的 `requires-python`）。

**时区必须是 `Asia/Shanghai`**：

```bash
sudo timedatectl set-timezone Asia/Shanghai
timedatectl | grep "Time zone"
```

两条 timer 的 `OnCalendar` 走的是**本地时间**，单元里没有 `Timezone=`。
云服务器默认 UTC，不改的话 `Mon..Fri 20:00` 会在北京时间次日凌晨 4 点触发——
那时候 Tushare 当天的数据早出全了，同步照样成功，`list-timers` 也一切正常，
只是你以为的"收盘后同步"变成了"凌晨同步"，中间十几个小时库里是旧数据。

## 1. 代码与 Python 依赖

```bash
git clone https://github.com/artist-coding/quant-monitor.git ~/quant-monitor
cd ~/quant-monitor
pip install --user -r requirements.txt
pip install --user -e .          # 装 zt / zt-web / zt-monitor 入口，落在 ~/.local/bin
```

`~/.local/bin` 不在 PATH 的话补一句 `export PATH="$HOME/.local/bin:$PATH"` 到 `~/.bashrc`。

### 装在哪里决定了单元怎么写

四份跑 Python 的单元（api / sync / amv）都是这两行：

```ini
Environment=HOME=/home/zx
ExecStart=/usr/bin/python3 ...
```

系统 python 靠 `HOME` 找到 `~/.local/lib/pythonX.Y/site-packages`，所以 `pip install --user` 装的包能被找到。
**这条链路很容易在新机上断掉，而且断了以后报的是 `ModuleNotFoundError`，看着像代码问题**：

- **Ubuntu 24.04 / Debian 12 起**，PEP 668 会让 `pip install --user` 直接失败
  （`error: externally-managed-environment`）。两个选择：
  - 用 venv（推荐），但**必须同步改四份单元的 `ExecStart`**，把 `/usr/bin/python3`
    换成 `$REPO/.venv/bin/python`。漏改哪一份，那一份就 `ModuleNotFoundError`。
  - 或者 `pip install --user --break-system-packages -r requirements.txt`，保持单元不动。
- 用 venv 的话，第 4 步的 sed 后面再追一条：

  ```bash
  sed -i "s#^ExecStart=/usr/bin/python3#ExecStart=$REPO/.venv/bin/python#" \
      ~/.config/systemd/user/quant-monitor-{api,sync,amv}.service
  ```

  `quant-monitor-web.service` 跑的是 node，不用改。

依赖版本对不齐时看 `requirements.core.txt`（本机实际跑着的关键包的确切版本）
和 `requirements.lock.txt`（全量快照）。

## 2. `.env`

```bash
cp .env.example .env
chmod 600 .env
```

至少填这几项，其余可选项 `.env.example` 里都有注释：

```env
DATA_MODE=jnb
DB_PATH=data/stock_data.db
TUSHARE_TOKEN=...
TUSHARE_API_URL=...
LLM_API_KEY=...            # 不填则不出 LLM 点评，其余功能不受影响
AMV_BAIDU_SHARE_URL=...    # 要跑活跃市值 timer 才需要
AMV_BAIDU_SHARE_PWD=...
```

Token 别走 git，也别贴进单元文件——`scp` 过去或者手打。

单元里写的是 `EnvironmentFile=<仓库>/.env`，**刻意没带 `-`**：文件不存在时
systemd 直接拒绝启动这个单元。看着像坏了，其实是设计——带 `-` 的话服务会照常
running 但 `TUSHARE_TOKEN` 是空的，同步和 LLM 全部静默失效，那才是真的难查。

## 3. 数据：搬库还是从零拉

### 路线 A：从备份恢复（推荐）

快照在私有仓库 `artist-coding/quant-monitor-data` 的 Release 里，按日期打 tag。
需要 `gh` 且已登录（`gh auth login`），或者在浏览器里下载再传上去。

```bash
cd ~/quant-monitor
gh release list --repo artist-coding/quant-monitor-data | head        # 挑最新的 tag
gh release download data-YYYYMMDD --repo artist-coding/quant-monitor-data -D /tmp/qm-restore

cd /tmp/qm-restore && sha256sum -c SHA256SUMS.txt                     # 先验校验和
mkdir -p ~/quant-monitor/data
zstd -d stock_data_YYYYMMDD.db.zst -o ~/quant-monitor/data/stock_data.db
zstd -dc data_manual_YYYYMMDD.tar.zst | tar -xf - -C ~/quant-monitor/data/   # amv / kimi_research / daily_scans
```

恢复完确认一下库是完整的：

```bash
cd ~/quant-monitor && python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:data/stock_data.db?mode=ro", uri=True)
print("integrity:", c.execute("PRAGMA integrity_check").fetchone()[0])
for t in ("daily_kline", "trade_cal", "amv_daily", "stock_basic"):
    print(t, c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
PY
```

**想直接从旧机 `scp` 也行，但不能对着运行中的库 `cp`**：uvicorn 常驻连着它，
直接复制可能拿到写到一半的页，得到一个能打开、`integrity_check` 也可能过、
但内容错乱的文件。要么先 `systemctl --user stop quant-monitor-api`，
要么用 `scripts/backup_data.sh --local-only` 出快照（走 SQLite 在线备份 API）再传。

### 路线 B：从零拉

```bash
zt sync init                                          # 建表
zt sync trade-cal --start 20170101 --end 20171231     # 一次一年
# 等满一小时，再拉下一年
zt sync trade-cal --start 20180101 --end 20181231
# ... 一直到明年
```

**这一步要跑好几天，早点开始。** `trade_cal` 是**两层配额叠加**（2026-09-01 实测，
上游两条原文都见过）：

| 限额 | 原文 |
| --- | --- |
| **1 次/小时** | 「您访问接口(trade_cal)频率超限(1次/小时)」 |
| **5 次/天** | 「您访问接口(trade_cal)频率超限(5次/天)」 |

**而且被拒的请求同样计入日额度**——手快连试几次，当天就没机会补救了，只能等次日。
所以：一次一年，每次之间隔 62 分钟，一天最多补 4~5 年，十年的日历要分三天跑完。
写个脚本挂着，别守在终端前。

**每拉一年都要核对行数。** 一年 365/366 行（`trade_cal` 存的是自然日，`is_open` 有 0 有 1），
不足就是被限流了，等一小时重来。被限流时当场是有报错的——日志里能看到上游原文
「您访问接口(trade_cal)频率超限(1次/小时)」，`sync_trade_cal` 把它转成返回 0 行、
CLI 报 `"status": "failed"`。**真正难查的是事后**：那一年整年没写进去，
`MIN`/`MAX(cal_date)` 看着完全正常。所以要**按年查，别只看 `MAX(cal_date)`**：

```bash
python3 -c "
import sqlite3; c=sqlite3.connect('data/stock_data.db')
for y,n in c.execute(\"SELECT substr(cal_date,1,4), COUNT(*) FROM trade_cal WHERE exchange='SSE' GROUP BY 1 ORDER BY 1\"): print(y,n)"
```

整年整年地缺是这个接口最典型的坏法，成因就是那个一小时的窗口：想一口气拉多年的话，
第一年成功、后面全被挡，而首尾两年还在，`MIN`/`MAX` 看着完全正常。
**这个库里就出过这个洞**——2026-09-01 查出 2018、2024、2025 三年是空的（其余年份齐全），
补的时候 2018 第一次就成，紧接着 2024 连试三次全被限流，才问出真实的限流规格。
日常日线同步不受影响（它只需要"库里最新日线 → 今天"这一段有日历），
但跨这几年的历史回补和任何拿 `trade_cal` 数交易日的地方都会算错。
走路线 A 恢复备份的话，这类洞会一起搬到新机上，装完照上面的方法按年核对一遍。

日历齐了再回补历史日线，用 `scripts/backfill_market_history.py`（按交易日走，支持断点续跑）。

**别指望 `scripts/sync_daily_kline.py` 帮你拉历史**：它只做增量，
`daily_kline` 是空表时会打一条 warning 然后直接退出（`pending_trade_days` 里那个
`last is None` 分支），systemd 记的还是 success。看日志会以为"跑了但没数据"。

活跃市值不用单独搬：上游是全量表、整表 upsert，`python3 scripts/sync_amv.py` 跑一次
就把 1993 年至今的 8000 多行全拿回来了。

## 4. 装单元：先把路径改掉

八份单元里的 `/home/zx` 和 `/home/zx/quant-monitor` 全是写死的。用户名或仓库位置一变就得重写：

```bash
cd ~/quant-monitor
REPO="$PWD"
install -d ~/.config/systemd/user
for f in deploy/*.user; do
    sed -e "s#/home/zx/quant-monitor#$REPO#g" -e "s#/home/zx#$HOME#g" \
        "$f" > ~/.config/systemd/user/"$(basename "$f" .user)"
done
grep -rl "/home/zx" ~/.config/systemd/user/ && echo "还有漏网的" || echo "路径已全部替换"
```

顺序不能反：先替换长的那条（带 `/quant-monitor`），否则 `/home/zx` 先被换掉，
剩下的 `/quant-monitor` 会拼成错的路径。

然后照 `deploy/README.md` 的步骤 `daemon-reload` + `enable --now`，
以及那份文档反复强调的一次性动作：

```bash
sudo loginctl enable-linger $USER
loginctl show-user $USER -p Linger      # 必须是 Linger=yes
```

没开 linger 时 `list-timers` 照常显示下次触发时间，看着一切正常，
但登出后用户实例就被杀，到点根本不会跑，也没有任何报错。

## 5. 端口

8010 这个选择是历史原因：**本机 8000 被其他用户占了**。新机上 8000 空着也建议继续用 8010——
省得再改一遍配置。真要改的话，三处必须同时改，漏一处就是页面能开但 `/api` 全 404：

1. `quant-monitor-api.service` 的 `ExecStart --port`
2. `frontend/vite.config.ts` 的 `server.proxy`（dev，5173）
3. `frontend/vite.config.ts` 的 `preview.proxy`（生产预览，4173）

第 2、3 条是两份独立配置：**`vite preview` 不继承 `server.proxy`**，只改一处的话
dev 模式好好的、生产模式全 404。

## 6. 前端

```bash
npm --prefix frontend install
npm --prefix frontend run build      # 服务托管的是 dist/，不 build 就没有产物
```

以后每次改前端代码都要重新 build，`systemctl --user restart quant-monitor-web` 不会重新编译。

### 想从别的设备访问

现在前后端都只绑 `127.0.0.1`，只有本机能开。要远程访问，改前端单元的 `ExecStart` 加 `--host`：

```ini
ExecStart=/usr/bin/node .../vite.js preview --port 4173 --host 0.0.0.0
```

**但后端不要跟着改成 `0.0.0.0`。** 这套 API 没有任何鉴权——没有 `Depends`、
没有 API key、没有 Authorization 检查，却有 21 个写接口，包括 `DELETE /themes/{name}`
和会触发 LLM 调研（花钱）的 `POST /daily/scan`。前端走 vite 的 `/api` 代理转发到
`127.0.0.1:8010`，后端不需要对外监听。

云服务器上尤其注意这两条：

- **`zt-web` 这个入口会绑 `0.0.0.0`**（`api/main.py` 的 `start_web()`），
  但它打印出来的地址是 `http://127.0.0.1:...`——打印的和实际绑的不一致。
  端口取 `API_PORT`，默认 8000。在有公网 IP 的机器上手工跑一次 `zt-web`，
  就等于把一个无鉴权 API 挂到公网上了。systemd 单元里走的是显式
  `uvicorn --host 127.0.0.1`，没有这个问题，手工调试时才要留神。
- 暴露 4173 前先想清楚：`vite preview` 是开发用的静态服务器，不是给公网用的。
  优先走 Tailscale（本机 `100.x` 那个地址，只有自己的设备连得上），
  要真开到公网就前面挂 nginx + 认证，并用防火墙把 8010 关死。

另外，前端如果改成直连后端（不走 vite 代理），要把 `4173` 加进 `CORS_ORIGINS`——
默认值只有 `http://localhost:5173,http://localhost:3000`。走代理时浏览器看到的是同源，
碰不到 CORS，所以这个默认值一直没暴露问题。

## 7. 活跃市值：新机独有的两个坑

BaiduPCS-Go 的安装、`-cookies` 登录、STOKEN 验证，照 `deploy/README.md` 的"一次性准备"做。
换机器时额外注意：

- **别把旧机的 `~/.config/BaiduPCS-Go/pcs_config.json` 拷过来当长久之计**。
  它是明文 Cookie，而且 1~3 个月就过期，拷过来最多撑到过期那天。重新登一次更省事。
  真拷了记得 `chmod 600`。
- **两台机器不要同时开这条 timer**。网盘的中转目录 `/auto_dl` 是账号级共享的，
  脚本每次运行会**清空**它；两边同时跑会互相删掉对方正在转存的文件，
  表现是随机失败，日志里看不出所以然。迁移期间要么把旧机的
  `quant-monitor-amv.timer` disable 掉，要么给新机在 `.env` 里换一个
  `AMV_BAIDU_PAN_WORKDIR`（比如 `/auto_dl_2`）。

日线同步没有这个问题——两台机器各写各的库，互不干扰，只是各自多打一份 Tushare 请求。

## 8. 备份

`scripts/backup_data.sh` 有两处默认值是给旧机（root 部署）写的：

```bash
BACKUP_DIR=~/backups/quant-monitor scripts/backup_data.sh    # 默认是 /root/backups/quant-monitor
```

还要 `gh` 并已登录（`gh auth status`），否则脚本在上传那步才 die。
只想在本地出快照就加 `--local-only`。

**同样别两台机器都挂**：Release 按日期打 tag，两边都跑会 `--clobber` 互相覆盖同一个 tag 的附件，
最后留下的是哪台机器的库全看谁跑得晚。

## 9. 验收

```bash
# 服务
systemctl --user is-active quant-monitor-api quant-monitor-web   # 两个 active
curl -s -o /dev/null -w "api %{http_code}\n"      http://127.0.0.1:8010/docs   # 200
curl -s -o /dev/null -w "frontend %{http_code}\n" http://127.0.0.1:4173/       # 200

# timer（注意 enable 的是 timer 不是 service）
systemctl --user list-timers 'quant-monitor-*'    # 两条都有下次触发时间
loginctl show-user $USER -p Linger                # Linger=yes

# 数据
zt sync status
zt amv status                                     # 当前区间，选股的总开关
python3 -c "
import sqlite3; c=sqlite3.connect('file:data/stock_data.db?mode=ro',uri=True)
print('kline', *c.execute('SELECT MIN(trade_date),MAX(trade_date) FROM daily_kline').fetchone())
print('cal  ', *c.execute('SELECT MIN(cal_date),MAX(cal_date) FROM trade_cal').fetchone())
print('amv  ', *c.execute('SELECT MAX(trade_date) FROM amv_daily').fetchone())"

# 端到端跑一次（都是幂等的，随时可以手工触发）
systemctl --user start quant-monitor-sync.service
journalctl --user -u quant-monitor-sync.service -n 30
zt analyze 000001.SZ --days 120
```

交易日历除了看 `MAX(cal_date)` 覆盖到今年年底，还要**按年数一遍行数**（见路线 B 那段脚本，
一年应该是 365/366 行）——整年缺失时首尾都正常，只有分年统计看得出来。
每年底记得跑一次 `zt sync trade-cal --start <明年>0101 --end <明年>1231`——
日历没覆盖到当天时，日线同步会拒绝执行（宁可不同步也不瞎猜），日志里有明确报错。

## 10. 旧机收尾

新机验收通过后，旧机上至少要停掉活跃市值那条（`/auto_dl` 会打架）：

```bash
systemctl --user disable --now quant-monitor-amv.timer
```

日线同步和备份不停也不会坏，只是白跑。想彻底停：

```bash
systemctl --user disable --now quant-monitor-{api,web} quant-monitor-{sync,amv}.timer
```

## 附：代理

补一条和机器强相关的：**本机 socks5 代理会打断数据同步的两条通路**，
而且报错都指向错误方向（表现为连接超时，看着像对端挂了）。
两条同步单元里都写了 `UnsetEnvironment=` 清代理，脚本里还有一层兜底，两处都别删。
在新机上手工跑同步脚本调试时，先确认当前 shell 没有 `http_proxy` / `all_proxy`。
