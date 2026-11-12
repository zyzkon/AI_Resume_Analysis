<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { fmtDate, fmtDuration } from '../utils'
import ScoreRing from '../components/ScoreRing.vue'
import StatusPill from '../components/StatusPill.vue'

const props = defineProps({ id: { type: String, required: true } })

const data = ref(null)
const loading = ref(true)
const error = ref('')

const result = computed(() => data.value?.result ?? null)
const duration = computed(() =>
  fmtDuration(data.value?.started_at, data.value?.finished_at),
)

const PRIORITY_TEXT = { high: '优先', medium: '其次', low: '可选' }
const CHANGE_TEXT = { improved: '已补齐', worsened: '本次缺失', unchanged: '未变', unknown: '未检查' }

async function load() {
  error.value = ''
  try {
    data.value = await api.getAnalysis(Number(props.id))
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <main class="page stack" style="gap: 18px">
    <div class="row wrap">
      <RouterLink
        v-if="data"
        class="dim"
        style="font-size: 13px"
        :to="{ name: 'resume', params: { id: data.resume_id } }"
      >
        ← 返回这份简历
      </RouterLink>
    </div>

    <div v-if="error" class="alert alert--error">{{ error }}</div>
    <div v-if="loading" class="empty">加载中…</div>

    <template v-else-if="data">
      <!-- 概览 -->
      <section class="card card__pad stack">
        <div class="row wrap">
          <h1 style="font-size: 19px">第 #{{ data.id }} 次分析</h1>
          <StatusPill :status="data.status" />
          <div class="spacer" />
          <span class="dim" style="font-size: 12.5px">{{ fmtDate(data.created_at) }}</span>
        </div>

        <div v-if="result" class="score-row">
          <ScoreRing :value="result.overall_score" label="综合评分" :size="76" color="#2f6bff" />
          <ScoreRing :value="result.match_score" label="岗位匹配度" :size="76" color="#0f9d58" />
          <div class="spacer" />
          <div class="meta" style="flex-direction: column; gap: 2px; align-items: flex-end">
            <span>{{ data.model_name || '未知模型' }} · {{ data.prompt_version }}</span>
            <span v-if="duration">耗时 {{ duration }}</span>
          </div>
        </div>

        <p v-if="result" class="muted" style="margin: 0">{{ result.summary }}</p>

        <div v-else class="alert alert--info">
          <template v-if="data.status === 'failed'">
            这次分析失败了：{{ data.error_message || '未记录原因' }}。回到简历页可以重新提交一次。
          </template>
          <template v-else> 分析还没跑完，稍等几秒刷新页面（或回到简历页看进度）。 </template>
        </div>

        <div v-if="data.jd_text" class="jd">
          <div class="dim" style="font-size: 12px; margin-bottom: 4px">目标岗位 JD</div>
          <div class="jd__text">{{ data.jd_text }}</div>
        </div>
      </section>

      <template v-if="result">
        <!-- 栏目检查 -->
        <section class="card card__pad">
          <div class="section-title">
            <h2>栏目完整性</h2>
            <span class="dim" style="font-size: 12.5px">
              缺 {{ result.completeness.filter((c) => !c.present).length }} 项
            </span>
          </div>
          <div class="check-grid">
            <div
              v-for="check in result.completeness"
              :key="check.name"
              class="check"
              :class="{ 'check--missing': !check.present }"
            >
              <div class="check__name">
                <span>{{ check.name }}</span>
                <span class="pill" :class="check.present ? 'pill--done' : 'pill--failed'">
                  {{ check.present ? '有' : '缺' }}
                </span>
              </div>
              <div class="check__comment">{{ check.comment }}</div>
            </div>
          </div>
        </section>

        <!-- 亮点与短板 -->
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px" class="two-col">
          <section class="card card__pad">
            <div class="section-title"><h2>亮点</h2></div>
            <ul class="list">
              <li v-for="(item, index) in result.highlights" :key="index">{{ item }}</li>
            </ul>
          </section>
          <section class="card card__pad">
            <div class="section-title"><h2>短板</h2></div>
            <ul class="list">
              <li v-for="(item, index) in result.weaknesses" :key="index">{{ item }}</li>
            </ul>
          </section>
        </div>

        <!-- 匹配度说明 -->
        <section class="card card__pad">
          <div class="section-title"><h2>匹配度依据</h2></div>
          <p class="muted" style="margin: 0">{{ result.match_reason }}</p>
        </section>

        <!-- 建议 -->
        <section class="card card__pad">
          <div class="section-title">
            <h2>修改建议</h2>
            <span class="dim" style="font-size: 12.5px">{{ result.suggestions.length }} 条</span>
          </div>

          <div class="suggestion" v-for="(item, index) in result.suggestions" :key="index">
            <div class="suggestion__head">
              <span class="prio" :class="`prio--${item.priority}`">
                {{ PRIORITY_TEXT[item.priority] || item.priority }}
              </span>
              <span class="pill">{{ item.category }}</span>
            </div>
            <div style="font-size: 13.5px">{{ item.issue }}</div>
            <div class="muted" style="font-size: 13px; margin-top: 4px">{{ item.advice }}</div>
            <div v-if="item.example" class="example">{{ item.example }}</div>
          </div>
        </section>
      </template>
    </template>
  </main>
</template>

<style scoped>
.jd {
  border-top: 1px solid var(--border);
  padding-top: 12px;
}

.jd__text {
  font-size: 13px;
  color: var(--text-2);
  white-space: pre-wrap;
  max-height: 120px;
  overflow: auto;
}

@media (max-width: 720px) {
  .two-col {
    grid-template-columns: 1fr !important;
  }
}
</style>
