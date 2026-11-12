import { createRouter, createWebHistory } from 'vue-router'
import { auth } from './auth'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('./views/LoginView.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'resumes',
    component: () => import('./views/ResumeListView.vue'),
  },
  {
    path: '/resumes/:id',
    name: 'resume',
    component: () => import('./views/ResumeDetailView.vue'),
    props: true,
  },
  {
    path: '/resumes/:id/compare',
    name: 'compare',
    component: () => import('./views/CompareView.vue'),
    props: true,
  },
  {
    path: '/analyses/:id',
    name: 'analysis',
    component: () => import('./views/AnalysisView.vue'),
    props: true,
  },
  // 兜底：手输错地址时回首页，而不是白屏
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  // history 模式（没有 #）需要服务器把所有路径都回退到 index.html。
  // Vite 的开发服务器自动做了这件事；部署时记得配 nginx 的 try_files
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const loggedIn = Boolean(auth.token)

  if (to.meta.public) {
    // 已登录还去登录页，直接送走 —— 否则用户会以为要重新登录一次
    return loggedIn ? { name: 'resumes' } : true
  }

  if (!loggedIn) {
    // 记下原目标，登录成功后回去。只用于站内路径，不会被当成开放跳转
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  return true
})

export default router
