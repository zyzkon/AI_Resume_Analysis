/**
 * 一层很薄的 fetch 封装。
 *
 * 只解决四件事，多一件都不做：
 * 1. 拼基地址（后端在另一个端口，靠 VITE_API_BASE_URL 配）
 * 2. 自动带 Authorization 头
 * 3. 把后端的错误体翻译成人能看懂的一句话
 * 4. 401 时统一清 token（token 过期、账号被禁用都从这里走）
 *
 * 为什么不引 axios：这个项目只需要 GET / POST / DELETE 和 FormData，
 * fetch 全都有。多一个依赖就多一份要跟着升级的包。
 */

const BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

let onUnauthorized = () => {}

/** 由 auth.js 在启动时注册，避免 api 层反过来 import 业务状态（会形成循环依赖） */
export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

/**
 * FastAPI 的 `detail` 有两种形状：
 * - 业务抛出：字符串，直接展示
 * - 422 校验失败：数组，每项是 { loc: ["body","email"], msg: "...", type: "..." }
 * 原样丢给用户会是一坨 JSON，这里摊平成「email: 值不是有效的邮箱地址」。
 */
function formatDetail(detail) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (!item || typeof item !== 'object') return String(item)
      // 去掉 body / query 这类前缀，只留字段名
      const loc = (item.loc || []).filter((key) => !['body', 'query', 'path'].includes(key))
      const field = loc.join('.')
      return field ? `${field}：${item.msg}` : item.msg
    })
    return parts.join('；')
  }
  return null
}

export async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = {}

  if (auth) {
    const token = localStorage.getItem('token')
    if (token) headers.Authorization = `Bearer ${token}`
  }

  // FormData 必须让浏览器自己设置 Content-Type —— 它要拼 boundary，
  // 手写 'multipart/form-data' 反而会丢掉 boundary，后端直接解析失败
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(body)
  }

  let response
  try {
    response = await fetch(BASE + path, { method, headers, body })
  } catch {
    // fetch 只有网络层失败才 reject（断网、后端没起、被 CORS 预检拦下）
    throw new ApiError(0, '连不上后端，确认 API 已在 8000 端口启动')
  }

  if (response.status === 401 && auth) {
    onUnauthorized()
  }

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`
    try {
      const data = await response.json()
      detail = formatDetail(data.detail) || detail
    } catch {
      // 后端返回了非 JSON（比如网关的 HTML 错误页），用默认文案
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return null
  return response.json()
}

export const api = {
  register: (email, password) =>
    request('/auth/register', { method: 'POST', body: { email, password }, auth: false }),

  // 用 /auth/login 而不是 /auth/token：后者是 OAuth2 表单（form-urlencoded），
  // 前者收 JSON，前端写起来直接得多，效果完全一样
  login: (email, password) =>
    request('/auth/login', { method: 'POST', body: { email, password }, auth: false }),

  me: () => request('/auth/me'),

  listResumes: (page = 1, size = 50) => request(`/resumes?page=${page}&size=${size}`),

  uploadResume: (file) => {
    const form = new FormData()
    form.append('file', file)
    return request('/resumes', { method: 'POST', body: form })
  },

  getResume: (id) => request(`/resumes/${id}`),

  deleteResume: (id) => request(`/resumes/${id}`, { method: 'DELETE' }),

  listAnalyses: (resumeId, page = 1, size = 50) =>
    request(`/resumes/${resumeId}/analyses?page=${page}&size=${size}`),

  createAnalysis: (resumeId, jobDescription) =>
    request(`/resumes/${resumeId}/analyses`, {
      method: 'POST',
      body: { job_description: jobDescription || null },
    }),

  getAnalysis: (id) => request(`/analyses/${id}`),

  trend: (resumeId, limit = 50) => request(`/resumes/${resumeId}/analyses/trend?limit=${limit}`),

  compare: (resumeId, baseId, targetId) =>
    request(`/resumes/${resumeId}/analyses/compare?base_id=${baseId}&target_id=${targetId}`),
}
