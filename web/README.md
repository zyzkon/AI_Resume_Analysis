# 前端（Vue 3 + Vite）

后端在仓库根目录，这里只是界面层。**它不包含任何业务判断** —— 分数怎么算、建议怎么生成全在后端，前端只负责发请求和画页面。

## 启动

后端必须先跑起来（默认 `http://127.0.0.1:8000`）：

```bash
# 仓库根目录
uvicorn app.main:app --reload

# 本目录
npm install        # 第一次
npm run dev        # http://127.0.0.1:5173
```

浏览器打开 **http://localhost:5173** 或 **http://127.0.0.1:5173** 都可以。两个地址算两个不同的 Origin，后端的 `CORS_ORIGINS` 默认都放行了。

停掉前端用 `Ctrl+C`，不要在别的终端直接关掉窗口 —— Vite 会留下占用 5173 的进程，下次启动报「Port 5173 is already in use」。

## 目录结构

```
src/
├── main.js           入口：先补一次用户信息，再挂载
├── App.vue           顶栏 + 路由出口
├── router.js         路由表与登录守卫
├── api.js            fetch 封装：基地址、token、错误文案、401 登出
├── auth.js           登录状态（reactive），没有用 Pinia
├── utils.js          日期/大小/耗时格式化
├── styles.css        设计变量与公共类，没有 UI 库
├── components/
│   ├── ScoreRing.vue  分数圆环（纯 SVG）
│   ├── StatusPill.vue 四种状态徽标
│   └── TrendChart.vue 分数折线（纯 SVG，没用 echarts）
└── views/
    ├── LoginView.vue        登录 / 注册
    ├── ResumeListView.vue   简历列表、上传、删除
    ├── ResumeDetailView.vue 提交分析、轮询进度、走势、历史、选两条对比
    ├── AnalysisView.vue     单次结果：评分、栏目、亮点短板、建议
    └── CompareView.vue      两次分析的差异
```

## 依赖只有四个

`vue`、`vue-router`、`vite`、`@vitejs/plugin-vue`。四个东西是刻意没引的：

| 没引 | 为什么 |
|---|---|
| **axios** | 只需要 GET/POST/DELETE 和 FormData，`fetch` 全都有。多一个依赖就多一份要跟着升级的包，而 `api.js` 的封装本来就要自己写错误文案 |
| **Pinia** | 全局状态只有「当前用户 + token」两个字段，`reactive` 一个对象就够了 |
| **Element Plus / Naive UI** | 组件总共五种（按钮、输入框、卡片、徽标、折线），引一套 UI 库等于为了五个组件拖进来几十个 |
| **echarts / chart.js** | 只画两条 0~100 的折线，用 `viewBox` + `<polyline>` 三十行就够。echarts 压缩后也有几百 KB |

结果：生产构建产物 **96 KB**（gzip 37 KB），其中 Vue 运行时占大头。

## 几个设计决定

**token 存 localStorage，不存内存**。刷新页面不该掉登录。代价是 XSS 能偷走它 —— 这个前提下前端不渲染任何用户提供的 HTML（简历正文用 `{{ }}` 插值，不是 `v-html`），后端也只返回 JSON，所以取舍可接受。真要更严就得换成 HttpOnly cookie，那需要后端配合，且要打开 CORS 的 `allow_credentials`。

**登出只有一个入口**。任何请求收到 401，`api.js` 调用注册进去的回调清掉 token；`App.vue` 监听 token 变空，统一跳登录页。三处各写一遍跳转，必然出现「清两次、跳两次」的抖动。

**分析靠轮询，不用 WebSocket**。分析耗时几十秒、每份简历一天也就跑几次，2 秒问一次的成本可以忽略；后端也只有一个 `status` 字段，没有进度百分比可推。轮询有上限：2 秒 × 300 次 = 10 分钟，超时就停下并提示 —— 否则一条永远 `pending` 的记录会让「开始分析」按钮永久置灰，用户连重来一次都做不到。

**上传前先校验一遍扩展名和大小**。纯粹为省一次往返，真正把关的是后端那五道关。两边的阈值必须一致（5 MB、`.pdf` / `.docx`），否则会出现「前端放过去、后端 415」的困惑。

**对比要求「较新的一次」在右边**。`compare` 接口的 `delta` 是 `after - before`，顺序反了正负号会全部反过来 —— 数字看着正常，结论是错的。前端按 id 排序（自增，id 大 = 时间新），不靠用户勾选顺序。

## 常见问题

**控制台报 `blocked by CORS policy`**
后端 `.env` 里的 `CORS_ORIGINS` 没包含当前地址。注意 `http://localhost:5173` 与 `http://127.0.0.1:5173` 是两个不同的 Origin，端口、协议、结尾斜杠都必须精确一致。改完 `CORS_ORIGINS` 要**重启 API**。

**端口被占 / 自动跳到 5174**
`vite.config.js` 里设了 `strictPort: true`，端口被占会直接报错而不是换一个 —— 换了端口 CORS 白名单就对不上，表现是「页面能开、所有请求被拦」，比直接失败更难查。

**登录后立刻被踢回登录页**
token 过期或被禁用。`main.js` 在挂载前会问一次 `/auth/me`，失败就清 token，所以看到的是干净的登录页而不是闪一下。

**接口 200 但页面读不到数据**
先看控制台是否有 CORS 报错。CORS 失败时服务端其实照常执行、照常返回 200，只是少了 `Access-Control-Allow-Origin`，浏览器于是不把响应交给页面。
