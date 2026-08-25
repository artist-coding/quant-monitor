# 部署单元说明

四份 systemd 单元,按机器和权限选一套,不要混装。

| 文件 | 类型 | 跑在 | 端口 | 说明 |
| --- | --- | --- | --- | --- |
| `quant-monitor-api.service` | system | root | 8000 | 旧服务器,root 部署在 `/root/quant-monitor` |
| `quant-monitor-api.service.newserver` | system | zx | 8010 | 新机 system 级方案,需要 sudo |
| `quant-monitor-api.service.user` | **user** | zx | 8010 | 新机实际在用,不需要 sudo |
| `quant-monitor-web.service.user` | **user** | zx | 4173 | 前端生产构建,不需要 sudo |

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

## 三个会静默坏掉的点

1. **linger**:用户级服务在所有登录会话断开后会被停掉,且开机不自启。
   必须执行一次 `sudo loginctl enable-linger $USER`,
   用 `loginctl show-user $USER -p Linger` 确认是 `Linger=yes`。

2. **前端改完要重新 build**:服务托管的是 `dist/` 里的产物,
   只 `systemctl --user restart quant-monitor-web` 不会重新编译。

3. **`vite preview` 不继承 `server.proxy`**:漏配 `preview.proxy` 的话
   页面照常打开、但所有 `/api` 请求 404,看着像后端挂了。

## EnvironmentFile 为什么不带 `-`

单元里刻意写成 `EnvironmentFile=/home/zx/quant-monitor/.env` 而不是 `=-`。
带 `-` 时配置文件缺失会被静默忽略,服务照常 running 但
`TUSHARE_TOKEN` / `LLM_API_KEY` 全空,同步与 LLM 静默失效。
