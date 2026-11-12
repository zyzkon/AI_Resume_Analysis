<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import { fmtDate } from '../utils'

const props = defineProps({ id: { type: String, required: true } })
const route = useRoute()

const data = ref(null)
const loading = ref(true)
const error = ref('')

const resumeId = computed(() => Number(props.id))
const baseId = computed(() => Number(route.query.base))
const targetId = computed(() => Number(route.query.target))

const CHANGE_META = {
  new: { text: '新增', cls: 'pill--failed', hint: '上次没有，这次出现了' },
  persisted: { text: '仍存在', cls: 'pill--pending', hint: '按上次的建议改，但问题还在' },
  resolved: { text: '已解决', cls: 'pill--done', hint: '上次提的问题这次没有了' },
}

const PRIORITY_TEXT = { high: '优先', medium: '其次', low: '可选' }
const SECTION_TEXT = { improved: '已补齐', worsened: '本次缺失', unchanged: '未变', unknown: '未检查' }

function deltaOf(value) {
  if (value > 0) return { text: `+${value}`, cls: 'delta--up' }
  if (value < 0) return { text: `${value}`, cls: 'delta--down' }
  return { text: '0', cls: 'delta--flat' }
}

/** 建议按「新增 → 仍存在 → 已解决」排：用户先关心现在该改什么 */
const grouped = computed(() => {
  const order = ['new', 'persisted', 'resolved']
  return order
    .map((key) => ({
      key,
      items: data.value?.suggestions.filter((item) => item.change === key) ?? [],
    }))
    .filter((group) => group.items.length > 0)
})

async function load() {
  error.value = ''
  if (!baseId.value || !targetId.value) {
    error.value = '缺少对比参数，请从简历页重新选择两次分析'
    loading.value = false
    return
  }
  try {
    data.value = await api.compare(resumeId.value, baseId.value, targetId.value)
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
    <RouterLink class="dim" style="font-size: 13px" :to="{ name: 'resume', params: { id } }">
      ← 返回这份简历
    </RouterLink>

    <div v-if="error" class="alert alert--error">{{ error }}</div>
    <div v-if="loading" class="empty">加载中…</div>

    <template v-else-if="data">
      <!-- 两侧摘要 -->
      <section class="card card__pad">
        <div class="compare-head">
          <div>
            <div class="dim" style="font-size: 12px">基准（较早）</div>
            <div class="row" style="gap: 8px; margin-top: 4px">
              <strong>#{{ data.base.id }}</strong>
              <span class="dim" style="font-size: 12.5px">{{ fmtDate(data.base.created_at) }}</span>
            </div>
            <div class="meta" style="margin-top: 4px">
              <span>综合 {{ data.base.overall_score }}</span>
              <span class="meta__sep">·</span>
              <span>匹配 {{ data.base.match_score }}</span>
              <span class="meta__sep">·</span>
              <span>{{ data.base.suggestion_count }} 条建议</span>
            </div>
          </div>

          <div class="compare-arrow">→</div>

          <div style="text-align: right">
            <div class="dim" style="font-size: 12px">对比（较新）</div>
            <div class="row" style="gap: 8px; margin-top: 4px; justify-content: flex-end">
              <strong>#{{ data.target.id }}</strong>
              <span class="dim" style="font-size: 12.5px">{{ fmtDate(data.target.created_at) }}</span>
            </div>
            <div class="meta" style="margin-top: 4px; justify-content: flex-end">
              <span>综合 {{ data.target.overall_score }}</span>
              <span class="meta__sep">·</span>
              <span>匹配 {{ data.target.match_score }}</span>
              <span class="meta__sep">·</span>
              <span>{{ data.target.suggestion_count }} 条建议</span>
            </div>
          </div>
        </div>

        <div class="verdict">
          <div style="font-size: 14px">{{ data.stats.verdict }}</div>
          <div class="meta" style="margin-top: 6px">
            <span>新增问题 {{ data.stats.new_issues }}</span>
            <span class="meta__sep">·</span>
            <span>已解决 {{ data.stats.resolved_issues }}</span>
            <span class="meta__sep">·</span>
            <span>仍存在 {{ data.stats.persisted_issues }}</span>
          </div>
        </div>
      </section>

      <!-- 分数变化 -->
      <section class="card card__pad">
        <div class="section-title"><h2>分数变化</h2></div>
        <div class="score-deltas">
          <div v-for="item in data.scores" :key="item.key" class="delta-card">
            <div class="dim" style="font-size: 12.5px">{{ item.label }}</div>
            <div class="row" style="gap: 8px; margin-top: 4px">
              <span class="dim">{{ item.before }}</span>
              <span class="dim">→</span>
              <strong style="font-size: 20px">{{ item.after }}</strong>
              <span class="delta" :class="deltaOf(item.delta).cls">{{ deltaOf(item.delta).text }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- 栏目变化 -->
      <section v-if="data.sections.length" class="card card__pad">
        <div class="section-title">
          <h2>栏目变化</h2>
          <span class="dim" style="font-size: 12.5px">
            「未检查」表示那一次分析没评估这个栏目，不是缺失
          </span>
        </div>
        <div class="check-grid">
          <div
            v-for="item in data.sections"
            :key="item.name"
            class="check"
            :class="{ 'check--missing': item.change === 'worsened' }"
          >
            <div class="check__name">
              <span>{{ item.name }}</span>
              <span class="pill" :class="item.change === 'improved' ? 'pill--done' : item.change === 'worsened' ? 'pill--failed' : ''">
                {{ SECTION_TEXT[item.change] }}
              </span>
            </div>
          </div>
        </div>
      </section>

      <!-- 建议变化 -->
      <section v-if="grouped.length" class="card card__pad">
        <div class="section-title"><h2>建议变化</h2></div>

        <div v-for="group in grouped" :key="group.key" style="margin-bottom: 16px">
          <div class="row" style="gap: 8px; margin-bottom: 8px">
            <span class="pill" :class="CHANGE_META[group.key].cls">{{ CHANGE_META[group.key].text }}</span>
            <span class="dim" style="font-size: 12.5px">{{ CHANGE_META[group.key].hint }}</span>
            <span class="dim" style="font-size: 12.5px">· {{ group.items.length }} 条</span>
          </div>

          <div v-for="(item, index) in group.items" :key="index" class="suggestion">
            <div class="suggestion__head">
              <span v-if="item.priority_after" class="prio" :class="`prio--${item.priority_after}`">
                {{ PRIORITY_TEXT[item.priority_after] || item.priority_after }}
              </span>
              <span class="pill">{{ item.category }}</span>
              <span v-if="item.change === 'persisted'" class="dim" style="font-size: 11.5px">
                措辞相似度 {{ (item.similarity * 100).toFixed(0) }}%
              </span>
            </div>
            <div style="font-size: 13.5px">{{ item.issue }}</div>
            <div class="muted" style="font-size: 13px; margin-top: 4px">{{ item.advice }}</div>
            <div v-if="item.example" class="example">{{ item.example }}</div>
          </div>
        </div>
      </section>
    </template>
  </main>
</template>

<style scoped>
.verdict {
  margin-top: 16px;
  padding: 13px 15px;
  border-radius: var(--radius-sm);
  background: var(--primary-weak);
  border: 1px solid #d8e3ff;
  color: #1b4dbb;
}

.score-deltas {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.delta-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 11px 14px;
  background: var(--surface-2);
}
</style>
