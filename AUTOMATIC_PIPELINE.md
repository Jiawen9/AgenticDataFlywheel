# 自动 Pipeline

入口为 `/pipeline`。创建时选择未发布的业务批次、起点与模式；流程由 FastAPI 后台编排，离开页面不会中止。原来的浏览器计时演示记录不导入后台。

## 流程看板

看板沿用原来的七大模块：迭代评估、任务生成、轨迹采集、轨迹质检、数据发布、模型训练、模型发布，并保留十五个子步骤。当前自动执行范围为采集到发布及汇总，其余模块保持灰色，提示“尚未接入自动执行”。人工修正、COT 和汇总的进度与入口位于运行状态区。

图中节点是后台八个执行步骤的展示映射，不是新增作业。页面总结与轨迹树构建共享建树进度，Rubrics 生成与相对排序共享质检进度；实际质检工序为生成 Rubrics、轨迹评分及结果排序。按任务交替执行时仅突出当前工序，整批步骤成功后才标记其子节点完成。发布完成与汇总状态分开显示。

## 两种模式

- 人工修正：采集回传 → 预处理与标框 → 建树 → 质检 → 每任务一条 Top1 → 人工确认 → 按需 COT → 发布 → 看板汇总。修正页面可以更换同任务的入选轨迹；每任务必须且只能选一条。无需修改的轨迹同样完整导出。
- 自动发布：前四阶段相同，再按创建时的 `global_score >= threshold` 每任务选择一条。阈值范围 0～5，同分按稳定轨迹身份排序。没有达标轨迹的任务保留淘汰原因；全部不达标不发布、不关闭批次。

发布成功关闭整个批次，完整上游过程件、原始采集数据和冻结发布保留。最终 Excel 为单工作表，仅含入选轨迹的全部有效步骤；统计来源 JSON 与表格同步筛选。人工采集或入选本身不计作人工精修。自动接续时若已有来源有效的人工修改，也按稳定步骤身份叠加到实际入选轨迹；不创建虚构会话，未入选轨迹的编辑不进入发布。

## 使用与恢复

已有本地原始 Rollout 可在新建弹窗选择“接续已有结果”后点击“导入已有 Rollout”。校验并登记新批次后，创建 Pipeline 会自动执行尚未完成的预处理；导入不会提前调用模型。已发布批次不可复用为导入目标。详见 [已有 Rollout 导入](backend/ROLLOUT_IMPORTS.md)。

“下发采集”复制当前手机工厂配置并冻结本次手机、App、VLA 和采样选项；“接续已有结果”复用已登记的完整输入和当前有效结果。同批次最多一个未结束的 Pipeline，创建前已有独立作业须先完成。配置变化需终止当前流程后新建。

点击节点进入原业务模块，显示该步骤持久绑定的真实作业。未开始的节点等待后端绑定，不会因查看页面提交任务或创建会话。受管理批次不能在其它页面重复处理、改源或独立导出发布；只有人工修正关卡允许保存编辑和确认。权限在后端检查，删掉路由参数不能绕过。

暂停只停止后续派发；已有作业继续完成。终止等待已启动作业结束后释放批次管理关系。采集下发已尝试但响应丢失时，必须先由回传服务核对远端状态，不把本地请求失败当成远端已经停止。当前采集失败或缺失任务阻止推进；补采后可以通过新流程接续完整有效输入，历史失败记录保留。

重启后按持久作业、产物和发布记录核对。有效结果复用，中断且缺少有效结果的阶段显示失败供重试。发布事务已提交但响应丢失时复用同一发布，不删除冻结文件。汇总失败不撤销发布，Pipeline 和发布详情都可重试转换；两边状态保持同步。

人工确认冻结轨迹选择、来源和人工修改摘要。自动 COT 不强制覆盖手写文本，生成结果的正常回写不使确认失效。

已终止或无可发布数据的历史 Pipeline 保留运行记录。为避免当前批次后续更新与旧作业混在一起，历史节点只展示该次运行记录；继续处理使用普通模块或新 Pipeline。已发布节点提供冻结发布详情。

## 接口与存储

| 接口 | 用途 |
|---|---|
| `GET /api/pipelines?batch_id=…&active_only=true` | 流程列表及批次管理关系 |
| `POST /api/pipelines` | 创建并开始，携带 `request_id`、`name`、`batch_id`、`mode`、`start_mode` 及模式配置 |
| `GET /api/pipelines/{pipeline_id}` | 流程详情、步骤、绑定作业、修正会话和发布编号 |
| `POST /api/pipelines/{pipeline_id}/{action}` | `pause`、`resume`、`retry`、`confirm-correction`、`terminate` |

控制请求携带 `expected_revision`；确认修正额外携带 `session_revision`。冲突返回 409，前端保留草稿并提示刷新。相同创建请求编号只能对应相同配置。

状态存于现有 SQLite 的 `pipelines`、`pipeline_owners`、`pipeline_requests` 记录空间，发布意图存于 `release_intents`。子作业继续使用原作业服务，编排器独立于模型执行线程。没有历史数据迁移，不改既有发布和看板布局。

实现入口：`backend/pipelines.py`（编排及服务适配）、`backend/pipeline_access.py`（管理权限）、`backend/pipeline_release.py`（选择及冻结发布）、`frontend/src/views/PipelineView.vue`（流程页面）。

## 本地验证

后端关键测试：

```powershell
.\.venv\Scripts\python.exe -m unittest backend.tests.test_pipelines backend.tests.test_pipeline_release backend.tests.test_pipeline_runtime_integration
```

前端在 `frontend` 下运行 `npm test` 和 `npm run build`。`frontend/tests/pipeline.browser.cjs` 使用本地静态构建和完全模拟的 API 验证真实组件；需要可用的 Playwright 及 Chromium，依赖设置沿用现有浏览器回归脚本。

测试包括模拟 HTTP 采集归档回传、真实预处理/建树/质检作业管理器配模拟模型、人工修正与 COT、发布及真实 SQLite/Excel 汇总。验收产物位于 `backend_workspace/tmp/pipeline-validation`。首版不自动执行种子评估、任务扩增、训练、模型发布或 S3 上传。
