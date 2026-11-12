<script setup>
import { computed } from 'vue'
import { fmtShortDate, fmtDate } from '../utils'

/**
 * 纯 SVG 折线图，不引图表库。
 *
 * 只需要画「两条 0~100 的分数随时间变化」，用 echarts（几百 KB）属于杀鸡用牛刀。
 * viewBox + width:100% 让它自适应容器宽度，不用监听 resize。
 */
const props = defineProps({
  points: { type: Array, default: () => [] },
})

const W = 640
const H = 200
const PAD = { left: 30, right: 14, top: 16, bottom: 28 }

const innerW = W - PAD.left - PAD.right
const innerH = H - PAD.top - PAD.bottom

const ticks = [0, 25, 50, 75, 100]

const xAt = (index) => {
  if (props.points.length === 1) return PAD.left + innerW / 2
  return PAD.left + (index * innerW) / (props.points.length - 1)
}

const yAt = (score) => PAD.top + innerH * (1 - Math.max(0, Math.min(100, score)) / 100)

const series = [
  { key: 'overall_score', color: '#2f6bff', name: '综合评分' },
  { key: 'match_score', color: '#0f9d58', name: '岗位匹配度' },
]

const paths = computed(() =>
  series.map((item) => ({
    ...item,
    d: props.points
      .map((point, index) => `${index === 0 ? 'M' : 'L'}${xAt(index)},${yAt(point[item.key])}`)
      .join(' '),
  })),
)

const dots = computed(() =>
  series.flatMap((item) =>
    props.points.map((point, index) => ({
      key: `${item.key}-${index}`,
      cx: xAt(index),
      cy: yAt(point[item.key]),
      color: item.color,
      tip: `${fmtDate(point.created_at)} · ${item.name} ${point[item.key]}`,
    })),
  ),
)

/** 横轴最多标三个日期（首、中、尾），标多了会挤成一团 */
const xLabels = computed(() => {
  const total = props.points.length
  if (total === 0) return []
  const indexes = total === 1 ? [0] : total === 2 ? [0, 1] : [0, Math.floor((total - 1) / 2), total - 1]
  return [...new Set(indexes)].map((index) => ({
    x: xAt(index),
    text: fmtShortDate(props.points[index].created_at),
  }))
})
</script>

<template>
  <div>
    <div class="legend">
      <span v-for="item in series" :key="item.key" class="legend__item">
        <span class="legend__swatch" :style="{ background: item.color }" />
        {{ item.name }}
      </span>
      <span class="chart-hint">可左右滑动</span>
    </div>

    <div class="chart-scroll">
      <svg
        :viewBox="`0 0 ${W} ${H}`"
        width="100%"
        role="img"
        aria-label="分数走势"
        preserveAspectRatio="xMidYMid meet"
      >
      <!-- 横向网格线 + 左侧刻度 -->
      <g>
        <template v-for="tick in ticks" :key="tick">
          <line
            :x1="PAD.left"
            :x2="W - PAD.right"
            :y1="yAt(tick)"
            :y2="yAt(tick)"
            stroke="#eceef3"
            stroke-width="1"
          />
          <text :x="PAD.left - 8" :y="yAt(tick)" text-anchor="end" dominant-baseline="central" class="axis">
            {{ tick }}
          </text>
        </template>
      </g>

      <!-- 折线 -->
      <path
        v-for="line in paths"
        :key="line.key"
        :d="line.d"
        fill="none"
        :stroke="line.color"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      />

      <!-- 每个数据点，悬停显示完整时间 -->
      <circle
        v-for="dot in dots"
        :key="dot.key"
        :cx="dot.cx"
        :cy="dot.cy"
        r="3.5"
        :fill="dot.color"
        stroke="#fff"
        stroke-width="1.5"
      >
        <title>{{ dot.tip }}</title>
      </circle>

      <!-- 横轴日期 -->
      <text
        v-for="label in xLabels"
        :key="label.x"
        :x="label.x"
        :y="H - 8"
        text-anchor="middle"
        class="axis"
      >
        {{ label.text }}
      </text>
      </svg>
    </div>
  </div>
</template>

<style scoped>
.legend {
  display: flex;
  gap: 16px;
  margin-bottom: 4px;
  font-size: 12.5px;
  color: var(--text-2);
}

.legend__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.legend__swatch {
  width: 9px;
  height: 9px;
  border-radius: 3px;
}

.axis {
  font-size: 11px;
  fill: var(--text-3);
  font-family: inherit;
}

/* 手机屏幕上整张图会被等比缩到一半，11px 的刻度只剩 5px，读不出来。
   所以给一个最小宽度 + 横向滚动：宁可让用户划一下，也不要把字缩成噪点 */
.chart-scroll {
  overflow-x: auto;
}

.chart-scroll > svg {
  display: block;
  min-width: 460px;
}

/* 窄屏会横向滚动，得给个提示，否则用户以为图就是这么被截断的 */
.chart-hint {
  display: none;
  margin-left: auto;
  color: var(--text-3);
}

@media (max-width: 720px) {
  .chart-hint {
    display: inline;
  }
}
</style>
