import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端在 8010(本机 8000 被其他用户占用)。dev 和 preview 必须走同一份代理:
// vite 的 preview.proxy 不会继承 server.proxy,不显式复用的话生产模式下 /api 全 404。
//
// target 写 127.0.0.1 而不是 localhost: 本机 /etc/hosts 里 localhost 只解析到 ::1,
// 而后端绑的是 IPv4 127.0.0.1。写 localhost 时能通全靠 node 的 happy eyeballs 回退,
// 不该把代理链路押在这上面。
const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8010',
    changeOrigin: true,
  },
}

// host 同样显式写 127.0.0.1。vite 默认的 'localhost' 在这台机器上只监听 [::1],
// 于是 ssh -L 4173:127.0.0.1:4173 和编辑器的自动端口转发都连不上——
// 表现是服务明明 running、curl [::1] 也通,但转发到本地就是拒绝连接。
// 要从别的设备直连(而不是走 SSH 转发)才改 0.0.0.0,但那等于把无鉴权的
// /api 代理一起对外暴露,见 deploy/NEW_SERVER.md「想从别的设备访问」。
const LOOPBACK = '127.0.0.1'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: LOOPBACK,
    port: 5173,
    proxy: apiProxy,
  },
  preview: {
    host: LOOPBACK,
    port: 4173,
    proxy: apiProxy,
  },
})
