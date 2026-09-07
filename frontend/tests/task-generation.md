# 任务池 / 任务生成验收

在 `frontend` 中执行：

```text
npm test -- --run
npx vue-tsc --noEmit --incremental false -p tsconfig.app.json
npm run build
node tests/task-generation.browser.cjs
```

浏览器测试需要可被 Node 解析到的 Playwright 和已安装的 Microsoft Edge；可以通过 `NODE_PATH` 指向已有的 Playwright 安装，无需修改项目依赖。

脚本自动在随机本地端口提供 `dist` 静态文件，并拦截所有 API。生成、替换、编辑、删除、导出均使用内存模拟数据，不启动后端、不调用模型、不修改真实知识库或结果。脚本结束时自动关闭浏览器和临时服务。

覆盖导航分组、目录和搜索联动、局部全选、半选、跨场景选择、资源刷新与替换、409 冲突、提交及终态轮询、历史快照、前置任务分组、保存失败和离开保护、删除恢复、筛选与全量导出、快速切换、加载重试、兼容路由及 1500/1024/390px 布局。

截图和 `acceptance.json` 输出到被 Git 忽略的 `backend_workspace/task-generation-review/`。下载响应仅为模拟字节，不用于验证真实 Excel 内容；Excel 格式和服务端引用校验未在本轮修改。
