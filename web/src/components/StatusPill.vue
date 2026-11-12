<script setup>
import { computed } from 'vue'
import { statusText } from '../utils'

const props = defineProps({
  status: { type: String, required: true },
})

const cls = computed(() => `pill--${props.status}`)
</script>

<template>
  <span class="pill" :class="cls">
    <span class="dot" />
    {{ statusText(status) }}
  </span>
</template>
