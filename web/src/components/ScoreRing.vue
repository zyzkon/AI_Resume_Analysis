<script setup>
import { computed } from 'vue'

const props = defineProps({
  value: { type: Number, default: null },
  label: { type: String, default: '' },
  size: { type: Number, default: 64 },
  color: { type: String, default: '#2f6bff' },
})

const stroke = 6
// 半径要减掉线宽的一半，否则圆环会溢出 viewBox 被裁掉
const radius = computed(() => (props.size - stroke) / 2)
const circumference = computed(() => 2 * Math.PI * radius.value)
const percent = computed(() => Math.max(0, Math.min(100, props.value ?? 0)))
const offset = computed(() => circumference.value * (1 - percent.value / 100))
const center = computed(() => props.size / 2)
</script>

<template>
  <div class="score">
    <div class="score__ring" :style="{ width: `${size}px`, height: `${size}px` }">
      <svg :width="size" :height="size" :viewBox="`0 0 ${size} ${size}`">
        <circle
          :cx="center"
          :cy="center"
          :r="radius"
          fill="none"
          stroke="#ecedf2"
          :stroke-width="stroke"
        />
        <!-- 进度弧用 dasharray 画：周长当作一整段虚线，再用 dashoffset 把后面那段藏起来 -->
        <circle
          :cx="center"
          :cy="center"
          :r="radius"
          fill="none"
          :stroke="color"
          :stroke-width="stroke"
          stroke-linecap="round"
          :stroke-dasharray="circumference"
          :stroke-dashoffset="value == null ? circumference : offset"
          :transform="`rotate(-90 ${center} ${center})`"
          class="score__arc"
        />
      </svg>
      <span class="score__num" :style="{ fontSize: `${size * 0.34}px` }">
        {{ value ?? '—' }}
      </span>
    </div>
    <span v-if="label" class="score__label">{{ label }}</span>
  </div>
</template>

<style scoped>
.score {
  display: flex;
  align-items: center;
  gap: 12px;
}

.score__ring {
  position: relative;
  flex: none;
}

.score__num {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.score__arc {
  transition: stroke-dashoffset 0.6s ease;
}

.score__label {
  font-size: 12.5px;
  color: var(--text-2);
}
</style>
