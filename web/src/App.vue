<script setup>
import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { auth, clearToken } from './auth'

const router = useRouter()
const route = useRoute()

function logout() {
  clearToken()
  // 不在这里 push：清 token 会触发下面的 watch，跳转统一由它负责，
  // 两个地方各跳一次容易出现「先跳到 redirect 目标、再被踢回登录页」的抖动
}

// token 被清掉只有两种来源：点退出、或任何请求收到 401。
// 两种都该回登录页，所以监听状态而不是在各个地方分别处理
watch(
  () => auth.token,
  (token) => {
    if (!token && route.meta.public !== true) {
      router.replace({ name: 'login', query: { redirect: route.fullPath } })
    }
  },
)
</script>

<template>
  <header v-if="auth.token" class="topbar">
    <div class="topbar__inner">
      <RouterLink to="/" class="brand">
        <span class="brand__mark">简</span>
        <span>简历分析</span>
      </RouterLink>
      <div class="spacer" />
      <span v-if="auth.user" class="dim topbar__email">{{ auth.user.email }}</span>
      <button class="btn btn--ghost btn--sm" type="button" @click="logout">退出</button>
    </div>
  </header>

  <RouterView />
</template>
