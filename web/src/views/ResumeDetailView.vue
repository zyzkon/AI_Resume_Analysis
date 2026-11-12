<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { fmtDate, fmtSize } from '../utils'
import StatusPill from '../components/StatusPill.vue'
import TrendChart from '../components/TrendChart.vue'

const props = defineProps({ id: { type: String, required: true } })
const router = useRouter()

const resume = ref(null)
const analyses = ref([])
const trend = ref({ points: [] })
const loading = ref(true)
const error = ref('')
const notice = ref('')
const showText = ref(false)

const jd = ref('')
const submitting = ref(false)
const pollingId = ref(null)
const picked = ref([])

const resumeId = computed(() => Number(props.id))
const hasTrend = computed(() => trend.value.points.length >= 2)
const canCompare = computed(() => picked.value.length === 2)

let timer = null
let attempts = 0

// 2 秒一次、最多 300 次 = 10 分钟。正常分析几十秒就结束，
// 超过这个时长基本是 worker 没起或卡住了。
// 不设上限的话，一条永远 pending 的记录会让按钮一直显示「分析进行中」，
// 用户连重新提交的机会都没有
const MAX_ATTEMPTS = 300

function stopPolling() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  attempts = 0
  pollingId.value = null
}

/**
 * 轮询某条分析，直到它进入终态。
 *
 * 为什么不 Websocket / SSE：分析耗时几十秒、频率极低，
 * 每 2 秒问一次的成本可以忽略，而后端不用为它多维护一条长连接通道。
 * 真正的实时推送等有进度百分比需求时再说 —— 现在后端只有一个 status 字段。
 */
function watchAnalysis(id) {
  stopPolling()
  pollingId.value = id

  timer = setInterval(async () => {
    attempts += 1
    if (attempts > MAX_ATTEMPTS) {
      stopPolling()
      notice.value = '这条分析还在排队或已卡住，稍后刷新看看；也可以直接再提交一次'
      return
    }
    try {
      const data = await api.getAnalysis(id)
      if (data.status === 'done' || data.status === 'failed') {
        stopPolling()
        await refreshAnalyses()
        notice.value =
          data.status === 'done' ? '分析完成' : `分析失败：${data.error_message || '未知原因'}`
      }
    } catch (err) {
      // 轮询失败（比如 token 过期、后端重启）就停下，避免每 2 秒刷一条报错
      stopPolling()
      error.value = err.message
    }
  }, 2000)
}

async function refreshAnalyses() {
  const [list, points] = await Promise.all([
    api.listAnalyses(resumeId.value),
    api.trend(resumeId.value),
  ])
  analyses.value = list.items
  trend.value = points
}

async function load() {
  error.value = ''
  try {
    const [detail, list, points] = await Promise.all([
      api.getResume(resumeId.value),
      api.listAnalyses(resumeId.value),
      api.trend(resumeId.value),
    ])
    resume.value = detail
    analyses.value = list.items
    trend.value = points

    // 刷新页面时如果还有分析在跑，接着盯 —— 否则用户得自己再点一次刷新
    const unfinished = list.items.find((item) => item.status === 'pending' || item.status === 'running')
    if (unfinished) watchAnalysis(unfinished.id)
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function submit() {
  error.value = ''
  notice.value = ''
  submitting.value = true
  try {
    const created = await api.createAnalysis(resumeId.value, jd.value.trim())
    notice.value = '已提交，正在排队…'
    await refreshAnalyses()
    watchAnalysis(created.id)
  } catch (err) {
    error.value = err.message
  } finally {
    submitting.value = false
  }
}

function togglePick(item) {
  if (item.status !== 'done') return
  const index = picked.value.indexOf(item.id)
  if (index >= 0) {
    picked.value.splice(index, 1)
    return
  }
  picked.value.push(item.id)
  // 只保留最近勾选的两个，第三个进来时顶掉最早那个，
  // 比弹一句「最多选两个」省事
  if (picked.value.length > 2) picked.value.shift()
}

function goCompare() {
  const [a, b] = picked.value
  // id 自增，所以 id 小的就是先创建的那次。对比接口要求 base 是较早的一次，
  // 顺序反了 delta 的正负号会全反过来 —— 那比报错更难发现
  const base = Math.min(a, b)
  const target = Math.max(a, b)
  router.push({ name: 'compare', params: { id: props.id }, query: { base, target } })
}

onMounted(load)
onBeforeUnmount(stopPolling)
</script>

<template>
  <main class="page stack" style="gap: 18px">
    <div>
      <RouterLink to="/" class="dim" style="font-size: 13px">← 返回简历列表</RouterLink>
    </div>

    <div v-if="error" class="alert alert--error">{{ error }}</div>
    <div v-if="notice" class="alert alert--info">{{ notice }}</div>

    <div v-if="loading" class="empty">加载中…</div>

    <template v-else-if="resume">
      <!-- 简历信息 -->
      <section class="card card__pad">
        <div class="row wrap">
          <div style="min-width: 0">
            <h1 style="font-size: 19px; overflow: hidden; text-overflow: ellipsis">
              {{ resume.original_filename }}
            </h1>
            <div class="meta" style="margin-top: 6px">
              <span>{{ resume.char_count }} 字</span>
              <span class="meta__sep">·</span>
              <span>{{ fmtSize(resume.file_size) }}</span>
              <span class="meta__sep">·</span>
              <span>{{ fmtDate(resume.created_at) }}</span>
            </div>
          </div>
          <div class="spacer" />
          <button class="btn btn--sm" type="button" @click="showText = !showText">
            {{ showText ? '收起正文' : '看看解析出的正文' }}
          </button>
        </div>

        <pre v-if="showText" class="resume-text">{{ resume.raw_text }}</pre>
      </section>

      <!-- 提交分析 -->
      <section class="card card__pad stack">
        <div class="section-title" style="margin-bottom: 0">
          <h2>提交一次分析</h2>
          <span class="dim" style="font-size: 12.5px">JD 可留空，留空按通用技术岗标准评估</span>
        </div>

        <div class="field">
          <textarea
            v-model="jd"
            class="textarea"
            maxlength="2000"
            placeholder="把目标岗位的 JD 贴进来，匹配度会按它算（最多 2000 字）"
          />
        </div>

        <div class="row">
          <button class="btn btn--primary" type="button" :disabled="submitting || !!pollingId" @click="submit">
            {{ pollingId ? '分析进行中…' : submitting ? '提交中…' : '开始分析' }}
          </button>
          <span v-if="pollingId" class="pill pill--running"><span class="dot" />正在轮询 #{{ pollingId }}</span>
        </div>
      </section>

      <!-- 走势 -->
      <section v-if="hasTrend" class="card card__pad">
        <div class="section-title">
          <h2>分数走势</h2>
          <span class="dim" style="font-size: 12.5px">共 {{ trend.points.length }} 次已完成的分析</span>
        </div>
        <TrendChart :points="trend.points" />
      </section>

      <!-- 历史 -->
      <section class="card card__pad">
        <div class="section-title">
          <h2>分析历史</h2>
          <span class="dim" style="font-size: 12.5px">
            勾选两次已完成的分析，可以对比差异
          </span>
          <div class="spacer" />
          <button class="btn btn--sm" type="button" :disabled="!canCompare" @click="goCompare">
            对比选中（{{ picked.length }}/2）
          </button>
        </div>

        <div v-if="analyses.length === 0" class="empty" style="padding: 30px 16px">
          还没有分析记录。上面提交一次试试。
        </div>

        <div v-else class="history">
          <div v-for="item in analyses" :key="item.id" class="history__row">
            <input
              class="history__check"
              type="checkbox"
              :checked="picked.includes(item.id)"
              :disabled="item.status !== 'done'"
              :title="item.status === 'done' ? '选为对比' : '只有已完成的分析能参与对比'"
              @change="togglePick(item)"
            />

            <div class="history__main">
              <div class="row wrap" style="gap: 8px">
                <span class="dim" style="font-size: 12.5px">#{{ item.id }}</span>
                <span class="dim" style="font-size: 12.5px">{{ fmtDate(item.created_at) }}</span>
              </div>
              <div v-if="item.summary" class="history__summary">{{ item.summary }}</div>
              <div v-else-if="item.status === 'failed'" class="history__summary" style="color: var(--danger)">
                本次分析失败，可重新提交
              </div>
            </div>

            <div class="history__score">
              {{ item.overall_score ?? '—' }}
              <span class="dim" style="font-size: 11.5px; font-weight: 400">分</span>
            </div>

            <StatusPill class="history__pill" :status="item.status" />

            <RouterLink
              v-if="item.status === 'done'"
              class="btn btn--sm history__link"
              :to="{ name: 'analysis', params: { id: item.id } }"
            >
              详情
            </RouterLink>
            <span v-else class="dim history__link" style="font-size: 12.5px">—</span>
          </div>
        </div>
      </section>
    </template>
  </main>
</template>

<style scoped>
.resume-text {
  margin: 14px 0 0;
  padding: 14px 16px;
  max-height: 320px;
  overflow: auto;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
