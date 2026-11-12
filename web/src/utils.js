/** 几个纯展示用的格式化函数。后端一律返回 ISO 8601 UTC 时间。 */

const pad = (n) => String(n).padStart(2, '0')

/** 2026-11-12 15:04（按浏览器本地时区） */
export function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 11-12（图表横轴用，短一点才放得下） */
export function fmtShortDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function fmtSize(bytes) {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 两次时间点之间的耗时，用于展示「这次分析跑了多久」 */
export function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return null
  const ms = new Date(endIso) - new Date(startIso)
  if (!Number.isFinite(ms) || ms < 0) return null
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

const STATUS_TEXT = {
  pending: '排队中',
  running: '分析中',
  done: '已完成',
  failed: '失败',
}

export function statusText(status) {
  return STATUS_TEXT[status] || status
}
