<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { loadUser, saveToken } from '../auth'

const router = useRouter()
const route = useRoute()

const mode = ref('login')
const email = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

const isRegister = computed(() => mode.value === 'register')
const submitText = computed(() => (loading.value ? '请稍候…' : isRegister.value ? '注册并登录' : '登录'))

function switchMode(next) {
  mode.value = next
  error.value = ''
}

async function submit() {
  error.value = ''
  loading.value = true
  try {
    const address = email.value.trim()

    if (isRegister.value) {
      // 注册只负责建账号，不会发 token；紧接着登一次拿 token，
      // 用户就不用「注册成功 → 再输一遍密码」走两趟
      await api.register(address, password.value)
    }

    const token = await api.login(address, password.value)
    saveToken(token.access_token)
    // 顶上那栏要显示邮箱，而邮箱来自 /auth/me。
    // 不补这一步的话，登录后 auth.user 一直是 null，
    // 右上角空白，得手动刷一次页面才出现
    await loadUser()

    // 只接受站内路径。直接 router.push(任意字符串) 会把 query 里的外链当成站内路由，
    // 虽然 vue-router 不会跳出去，但也没必要给这种输入留口子
    const redirect = route.query.redirect
    const target = typeof redirect === 'string' && redirect.startsWith('/') ? redirect : '/'
    await router.replace(target)
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-wrap">
    <div class="card auth-card stack" style="gap: 18px">
      <div>
        <h1>简历分析</h1>
        <p class="muted" style="margin: 6px 0 0">上传简历，拿到结构化评分与逐条修改建议</p>
      </div>

      <div class="tabs">
        <button
          type="button"
          class="tabs__item"
          :class="{ 'tabs__item--active': mode === 'login' }"
          @click="switchMode('login')"
        >
          登录
        </button>
        <button
          type="button"
          class="tabs__item"
          :class="{ 'tabs__item--active': mode === 'register' }"
          @click="switchMode('register')"
        >
          注册
        </button>
      </div>

      <form class="stack" @submit.prevent="submit">
        <div class="field">
          <label for="email">邮箱</label>
          <input
            id="email"
            v-model="email"
            class="input"
            type="email"
            autocomplete="username"
            placeholder="you@example.com"
            required
          />
        </div>

        <div class="field">
          <label for="password">密码</label>
          <input
            id="password"
            v-model="password"
            class="input"
            type="password"
            :minlength="isRegister ? 8 : 1"
            :autocomplete="isRegister ? 'new-password' : 'current-password'"
            :placeholder="isRegister ? '至少 8 个字符' : '输入密码'"
            required
          />
        </div>

        <div v-if="error" class="alert alert--error">{{ error }}</div>

        <button class="btn btn--primary btn--block" type="submit" :disabled="loading">
          {{ submitText }}
        </button>
      </form>

      <p class="dim" style="margin: 0; font-size: 12px">
        后端需先启动（默认 http://127.0.0.1:8000），首次使用请先注册。
      </p>
    </div>
  </div>
</template>
