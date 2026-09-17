# 批次轨迹预处理

页面入口为“专家工作台 → 轨迹采集 → 轨迹预处理与建树”，路由仍是 `/collection/tree-building`。从手机采集点击“前往预处理”会带上 `collection_batch_id`，只定位批次，不自动运行。

## 操作流程

1. 生成或扩增结果提交采集后，沿用来源作业的 `batch_id`。
2. 采集端接收 `batch_id / collection_run_id / output_dir`，把最终轨迹写入指定目录并登记完成。具体协议见 [COLLECTION_RUNS.md](COLLECTION_RUNS.md)。
3. 在预处理页展开批次列表并选择一批。没有已登记可用轨迹时显示“等待采集结果”。
4. 点击“开始预处理”，后台按冻结清单执行转换、标框。转换不调用模型；初始 JSON 和 Excel 一旦发布即可下载，即使标框失败仍保留。
5. 标框完成后查看步骤、修改动作框，选择任务提交建树。Observation／中间态判断仍属于后续建树阶段。

批次列表包含已有01／02产物的批次，因此现有 validation 数据仍可查看和建树，不补造生成／采集记录。没有采集完成登记的已有轨迹批次不可通过新按钮重新扫描；CLI仍可用于明确指定的自备轨迹。

## 保存与身份

```text
backend_workspace/raw/collection_batches/<batch_id>/runs/<collection_run_id>/
  <collection_case_id>/<原轨迹目录>/原始文件
backend_workspace/system/preprocessing/<batch_id>/<job_id>/
  input.json
  trajectories_to_excel.xlsx
  annotated_trajectories.xlsx
backend_workspace/batches/<batch_id>/01_conversion/<version>/
backend_workspace/batches/<batch_id>/02_annotation/<version>/
```

作业及进度保存在 SQLite 的 `preprocessing_jobs`。输入文件清单先写入 `input.json`，其路径和SHA256保存到作业与01清单。内部转换／标框读取冻结 JSON，Excel仅供查看下载。缺失或损坏的JSON明确报错，不从Excel或目录扫描补回。

同批次多次采集累计已完成结果，作业启动时固定范围；后续采集留到下次处理。步骤JSON携带 `task_id、trajectory_id、source_trajectory_id、collection_run_id、collected_at、collection_case_id、source_result_id`。内部轨迹ID为 `tr_` 加 `SHA256(JSON[batch_id, collection_run_id, task_id, relative_dir])`，JSON编码使用UTF-8、无额外空格；步骤保留原编号。同名原轨迹不会因补采或跨批次而合并。

Excel保留原有业务列；JSON中的来源身份字段不会加到业务表头。图片和XML保存相对该批次原始根的真实路径，资源API使用批次及标框版本定位，不按全局任务名猜路径。

CLI默认工作文件也按批次及本次执行隔离保存，显式 `--export-output / --annotated-output` 仍尊重调用方指定位置：

```powershell
python backend/trajectories_preprocessing.py --source backend_workspace/raw/rollout_trajectories --batch-id my-processing-batch
```

## 作业API

| 接口 | 行为 |
| --- | --- |
| GET /api/trajectory-preprocessing/batches | 返回批次摘要、就绪数量、当前02版本、最新作业、产物和不可启动原因 |
| POST /api/trajectory-preprocessing/jobs | 请求 `{"batch_id":"..."}`，返回作业，HTTP202 |
| GET /api/trajectory-preprocessing/jobs?batch_id=... | 查询作业历史 |
| GET /api/trajectory-preprocessing/jobs/{job_id} | 恢复状态、当前阶段、真实步骤计数和错误 |
| POST /api/trajectory-preprocessing/jobs/{job_id}/retry | 使用原冻结输入重试，HTTP202 |

作业状态：`queued/running/succeeded/failed/interrupted`；执行阶段：`scanning/converting/annotating/publishing`。阶段进度使用 `completed_steps/total_steps`，附当前任务、原轨迹名和步骤号；发布完毕才将作业置为完成。扫描尚未得出总数时显示不确定进度。所有新增API响应禁用缓存。

`artifacts` 返回已登记的完整阶段清单，文件下载使用已有：

```text
GET /api/data-batches/<batch_id>/artifacts/<stage>/<version>/files/<filename>
```

不存在的批次／作业返回404；没有可用轨迹、清单冲突、文件校验失败、不可重试或配置变化返回409；请求结构错误返回422。

## 重复操作、恢复与并发编辑

- 同批次正在执行时重复启动返回现有作业。相同输入与处理配置已有成功结果时直接返回它，不重复调用模型。
- 标框失败后重试保留转换版本，复用相同批次和配置下有效的模型缓存；新采集不会混进旧作业。
- 重启把执行中作业标记为中断，用户主动重试。代码／模型配置变化时要求重新开始，避免混用不同规则。
- 同批次已有较新输入的成功结果时拒绝重试旧失败输入，防止当前结果倒退；相同输入已被成功重试则返回该成功作业。
- 新02发布前检查启动时的基准版本。预处理期间发生人工改框时保留人工版本并报告冲突；重试不能绕过这一检查，重新开始才采用新的基准。
- 轨迹读取与图片请求携带 `batch_id、annotation_version`；改框携带同样信息并校验预期版本，成功返回新版本。建树请求同时传这两个字段，提交时即冻结所选02。
- 建树提交、实际执行和发布前验证新采集输入清单及原文件，防止采集完成后图片被替换。已有validation没有该采集清单，维持既有产物校验，不伪造来源。
- 当前应用按单进程后端和共享后台队列部署。前端切换批次清理旧缓存与轮询，拒收晚回包，并保护未保存标框。

## 验证与接入边界

```powershell
python -m unittest backend.tests.test_preprocessing_jobs backend.tests.test_preprocessing_downstream_integration -v
python -m backend.tests.collection_run_demo
npm test --prefix frontend
npm run build --prefix frontend
```

测试使用临时数据和模拟模型／采集服务，不向真实手机下发。采集服务源码不在本仓库：这里只实现下发参数、接收完成清单、预处理及页面。采集端必须实际执行目录写入和完成登记；没有登记时页面不会把下发回执当成轨迹就绪。

