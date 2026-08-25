import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端在 8010(本机 8000 被其他用户占用)。dev 和 preview 必须走同一份代理:
// vite 的 preview.proxy 不会继承 server.proxy,不显式复用的话生产模式下 /api 全 404。
const apiProxy = {
  '/api': {
    target: 'http://localhost:8010',
    changeOrigin: true,
  },
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: apiProxy,
  },
  preview: {
    port: 4173,
    proxy: apiProxy,
  },
})
