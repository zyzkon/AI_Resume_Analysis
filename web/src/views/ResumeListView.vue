<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { fmtDate, fmtSize } from '../utils'

const MAX_MB = 5
const ALLOWED = ['.pdf', '.docx']

const items = ref([])
const loading = ref(true)
const uploading = ref(false)
const dragging = ref(false)
const error = ref('')
const fileInput = ref(null)

async function load() {
  error.value = ''
  try {
    const data = await api.listResumes()
    items.value = data.items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function upload(file) {
  if (!file) return
  error.value = ''

  // 前端先做一遍检查，纯粹是为了省一次往返 —— 后端那五道关才是真正的把关。
  // 两边的规则必须一致，否则会出现「前端放过去、后端 415」的困惑
  const name = file.name.toLowerCase()
  if (!ALLOWED.some((ext) => name.endsWith(ext))) {
    error.value = '只支持 .pdf 和 .docx 文件'
    return
  }
  if (file.size > MAX_MB * 1024 * 1024) {
    error.value = `文件不能超过 ${MAX_MB} MB，当前 ${fmtSize(file.size)}`
    return
  }

  uploading.value = true
  try {
    await api.uploadResume(file)
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    uploading.value = false
    // 清空 input，否则选同一个文件第二次不会触发 change 事件
    if (fileInput.value) fileInput.value.value = ''
  }
}

function pick(event) {
  upload(event.target.files?.[0])
}

function drop(event) {
  dragging.value = false
  upload(event.dataTransfer?.files?.[0])
}

async function remove(item) {
  const ok = window.confirm(`删除「${item.original_filename}」？它的分析记录会一起删除，且不可恢复。`)
  if (!ok) return
  try {
    await api.deleteResume(item.id)
    items.value = items.value.filter((row) => row.id !== item.id)
  } catch (err) {
    error.value = err.message
  }
}

onMounted(load)
</script>

<template>
  <main class="page stack" style="gap: 20px">
    <div class="row wrap">
      <div>
        <h1>我的简历</h1>
        <p class="muted" style="margin: 4px 0 0">
          上传 PDF 或 DOCX，分析会逐栏目检查并给出可执行的修改建议
        </p>
      </div>
      <div class="spacer" />
      <span v-if="!loading" class="pill">{{ items.length }} 份</span>
    </div>

    <div v-if="error" class="alert alert--error">{{ error }}</div>

    <div
      class="dropzone"
      :class="{ 'dropzone--active': dragging }"
      role="button"
      tabindex="0"
      @click="fileInput?.click()"
      @keydown.enter.prevent="fileInput?.click()"
      @keydown.space.prevent="fileInput?.click()"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="drop"
    >
      <input ref="fileInput" type="file" accept=".pdf,.docx" hidden @change="pick" />
      <div class="dropzone__title">
        {{ uploading ? '正在上传并解析…' : '点击选择，或把简历拖到这里' }}
      </div>
      <div class="dim" style="font-size: 12.5px; margin-top: 4px">
        支持 PDF / DOCX，最大 {{ MAX_MB }} MB。扫描件（图片型 PDF）提取不到文字
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>

    <div v-else-if="items.length === 0" class="empty">
      还没有简历。先上传一份，再提交分析。
    </div>

    <div v-else class="resume-grid">
      <article v-for="item in items" :key="item.id" class="card resume-card">
        <div>
          <div class="resume-card__name" :title="item.original_filename">
            {{ item.original_filename }}
          </div>
          <div class="meta" style="margin-top: 6px">
            <span>{{ item.char_count }} 字</span>
            <span class="meta__sep">·</span>
            <span>{{ fmtSize(item.file_size) }}</span>
            <span class="meta__sep">·</span>
            <span>{{ fmtDate(item.created_at) }}</span>
          </div>
        </div>

        <div class="row">
          <RouterLink class="btn btn--primary btn--sm" :to="{ name: 'resume', params: { id: item.id } }">
            查看分析
          </RouterLink>
          <div class="spacer" />
          <button class="btn btn--danger btn--sm" type="button" @click="remove(item)">删除</button>
        </div>
      </article>
    </div>
  </main>
</template>
