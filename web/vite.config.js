import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // 绑 IPv4 而不是默认的 localhost：Windows 上 localhost 先解析到 IPv6 的 ::1，
    // 只监听 ::1 的话 http://127.0.0.1:5173 会直接连不上（浏览器不会自动回退）。
    // 绑 127.0.0.1 之后两个地址都能进：localhost 解析失败时浏览器会退到 IPv4
    host: '127.0.0.1',
    port: 5173,
    // 端口被占用时直接报错，而不是悄悄换到 5174。
    // 后端 CORS 白名单只认 5173，自动换端口的表现是「页面能开、所有请求被拦」，
    // 那种错误从浏览器控制台看不出是端口的问题，所以宁可直接失败
    strictPort: true,
  },
})
