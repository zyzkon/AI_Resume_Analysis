/**
 * 登录状态。用 `reactive` 而不是 Pinia：
 * 整个前端只有「当前用户 + token」这一份共享状态，
 * 引一个状态库要多写 store 定义、多记一套 API，收益抵不过成本。
 */

import { reactive } from 'vue'
import { api, setUnauthorizedHandler } from './api'

const TOKEN_KEY = 'token'

export const auth = reactive({
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: null,
  ready: false,
})

export function saveToken(token) {
  auth.token = token
  // 存 localStorage 而不是内存：刷新页面不该掉登录。
  // 代价是 XSS 能偷走它 —— 所以前端不渲染任何用户提供的 HTML，
  // 后端也不返回 HTML，这个前提下 localStorage 是可接受的取舍
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken() {
  auth.token = ''
  auth.user = null
  localStorage.removeItem(TOKEN_KEY)
}

/** 启动时补一次用户信息。token 可能已过期，这里失败就直接清掉。 */
export async function loadUser() {
  if (!auth.token) {
    auth.ready = true
    return
  }
  try {
    auth.user = await api.me()
  } catch {
    clearToken()
  } finally {
    auth.ready = true
  }
}

// 任何请求收到 401 都会走到这里：统一登出。
// 注册在模块加载时执行，早于任何一次业务请求
setUnauthorizedHandler(() => {
  clearToken()
})
