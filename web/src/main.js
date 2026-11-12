import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { loadUser } from './auth'
import './styles.css'

const app = createApp(App)
app.use(router)

// 挂载之前先补一次用户信息：token 存在不代表有效（可能已过期或账号被禁用）。
// 先问一次 /auth/me，失败的会在这里被清掉，用户看到的是干净的登录页，
// 而不是「进了首页 → 闪一下 → 被踢回登录页」
loadUser().finally(() => {
  app.mount('#app')
})
