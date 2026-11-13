# 轨迹质检详情布局与最高 F 高亮

日期：2026-11-13。本次仅自用前端和说明文档；后端、CLI、采集服务、评分公式和轨迹原件均不改。沿用已经合入的本地质检和坐标接口，不重新采集或重算评分。

## 页面变化

- 桌面详情栏从 300px 调整为 `clamp(380px,32vw,480px)`，内部滚动；窄屏上下排列，截图保持比例。
- 评分卡局部深色配色。F、Rubric 维度、Checklist 通过数、结论及所有检查项摘要常驻；摘要最多两行，单项展开完整描述、理由和证据，同一时间展开一项。
- 按当前任务有效、未四舍五入的 F 高亮真实轨迹。并列最高一次突出一条，默认沿用已有 Top-1 排序，可切换；无有效 F 不高亮，部分评分显示覆盖数。
- 点击共享节点优先展示高亮轨迹截图，支持手动切换。后台刷新保留节点、轨迹选择、已展开证据和树视口；并发切换丢弃过期响应。

## 创新平台适配

不覆盖创新平台页面。按本仓库相关组件逐段移植布局、样式和交互，保留目标工程 API_BASE_URL、导入别名、路由前缀和组件体系。

| 代码 | 适配内容 |
| --- | --- |
| `QualityView.vue` | 详情栏宽度／滚动、高亮开关和并列选择、共享截图优先、刷新选择保持。移动端 `body` 最小宽度覆盖仅在质检页生效，不全站更改。 |
| `QualityTrajectoryScore.vue` | 局部深色主题、全部检查项摘要和单项证据展开；传入绑定批次、任务、树版本及轨迹的 `trajectoryKey`。 |
| `ReferenceTrajectoryTree.vue` | 接收可选 `highlightTrajectoryId`；标记真实祖先路径，黄色选中状态优先；拓扑不变时保留视口。 |
| `qualityScores.ts`、`types.ts` | 使用既有 `evaluations[id].fusion.valid/score`、`fusion_order/trajectory_order`、终点轨迹身份；原始 F 比较，显示才舍入。 |
| `useBatchQualityWorkspace.ts` | 刷新时不先清空详情；批次、任务、显式选择及刷新代次共同隔离旧响应。 |

没有新增 HTTP 接口、模型调用或依赖。不能用 Rubric 分代替缺失 F，不能把高亮选择写回发布选择。不要更改页面原有停止、重试、评分或发布资格。

坐标沿用原树 GET 路由的 `view=coordinate` 模式。同步时确认上一轮前后端均已部署；保留创新平台基址，不能盲目删除 `/v1`。现场核对响应为 JSON、步骤和版本身份正确，再对照截图实际点位；“提示不再重复出现”不算坐标验收通过。

## 定向验证与同步

本地验证已完成：10 个前端测试文件，84 项全部通过，无跳过；`vue-tsc -b` 类型检查和 Vite 生产构建通过。构建仍有依赖 PURE 注释及大 chunk 提示，不影响构建完成，本次未顺带调整全站打包。

定向回归覆盖：唯一／并列最高、未舍入比较、共享分支和终点、部分评分／无有效 F、单项展开／身份切换、选择及刷新竞争、缩放保持、坐标去重／手动重试／迟到响应及放大预览。执行命令（项目 `frontend` 目录）：

```powershell
npm test -- --configLoader runner src/utils/qualityScores.test.ts src/components/ReferenceTrajectoryTree.test.ts src/components/QualityTrajectoryScore.test.ts src/components/QualityFusionPresentation.test.ts src/views/QualityView.test.ts src/views/QualityViewHighlight.test.ts src/composables/useBatchQualityWorkspace.test.ts src/composables/useCoordinateInfo.test.ts src/components/ActionImage.test.ts src/coordinateApi.test.ts
npm run build -- --configLoader runner
```

隔离 Chrome 预览加载实际页面组件、ElementPlus 及全局样式，接口和截图均为本地夹具：1440px 右栏约 460.8px；390px 无横向溢出；6 项摘要、单项证据展开、青绿路径／黄色选中正常。共享终点选择最高 F 轨迹，放大复用坐标，失败只请求一次并保留原因与重试入口；运行时异常和外部请求均为 0。机器记录见 [validation.json](validation.json)。临时浏览器和预览服务已关闭，夹具未写入正式工程。

一次初始测试及一次构建因复用依赖目录的沙箱缓存写权限退出；使用 Vite runner 和获准的构建缓存写入后完成上述验证。没有安装新依赖，没有执行手机采集或模型调用。

仍待现场验证：真实轨迹在 icap 原 GET 路由下返回正确 JSON，并与实际截图点击／滑动位置一致；创新平台自身样式适配。离线坐标夹具通过不等于现场已修复。

将本次修改和新增测试一起同步。仅重新构建／启动对应前端，无需因本次 UI 修改重启后端或采集服务。创新平台由内网按上述范围适配；未做真实模型、真实手机或 icap 网关验收。

## 提交说明

标题：`feat(quality): 优化质检详情布局并高亮最高融合分轨迹`

总结：加宽质检详情栏，修复评分配色并将 Checklist 改为摘要常驻、证据单项展开；按有效 F 高亮真实最高分轨迹，支持并列切换和部分评分覆盖。保留评分刷新时的节点、截图及树视口，补充交互和坐标回归测试。仅前端与文档，不修改评分公式、后端或采集端。
