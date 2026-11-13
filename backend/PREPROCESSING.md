# 批次轨迹预处理

页面入口为“专家工作台 → 轨迹采集 → 轨迹预处理与建树”，路由仍是 `/collection/tree-building`。从手机采集点击“前往预处理”会带上 `batch_id`，只定位批次，不自动运行。

## 操作流程

1. 生成或扩增结果提交采集后，沿用来源作业的 `batch_id`。
2. 采集端接收 `batch_id / collection_run_id / output_dir`，把最终轨迹写入指定目录并登记完成。具体协议见 [COLLECTION_RUNS.md](COLLECTION_RUNS.md)。
3. 在预处理页展开批次列表并选择一批。没有已登记可用轨迹时显示“等待采集结果”。
4. 点击“开始预处理”，后台按冻结清单执行转换、标框。转换不调用模型；初始 JSON 和 Excel 一旦发布即可下载，即使标框失败仍保留。
5. 标框完成后查看步骤、修改动作框，选择任务提交建树。Observation／中间态判断仍属于后续建树阶段。

批次列表包含已有01／02产物的批次，因此现有 validation 数据仍可查看和建树，不补造生成／采集记录。没有采集完成登记的旧批次不可通过预处理按钮重新扫描。自备原始轨迹可使用“导入已有 Rollout”登记成新批次，再由预处理或 Pipeline 执行转换和标框；CLI仍可用于明确指定的自备轨迹。详见 [已有 Rollout 导入](ROLLOUT_IMPORTS.md)。

## 保存与身份

```text
backend_workspace/raw/collection_batches/<batch_id>/runs/<collection_run_id>/
  <collection_case_id>/<原轨迹目录>/原始文件
backend_workspace/system/preprocessing/<batch_id>/<job_id>/
  input.json
  # 计算JSON/Excel完成后清理，只保留输入清单
backend_workspace/batches/<batch_id>/01_conversion/
backend_workspace/batches/<batch_id>/02_annotation/
```

作业及进度保存在 SQLite 的 `preprocessing_jobs`。输入文件清单先写入 `input.json`，其路径和SHA256保存到作业与01清单。内部转换／标框读取冻结 JSON，Excel仅供查看下载。缺失或损坏的JSON明确报错，不从Excel或目录扫描补回。

同批次多次采集累计已完成结果，作业启动时固定范围；后续采集留到下次处理。步骤JSON携带 `task_id、trajectory_id、source_trajectory_id、collection_run_id、collected_at、collection_case_id、source_result_id`。内部轨迹ID为 `tr_` 加 `SHA256(JSON[batch_id, collection_run_id, task_id, relative_dir])`，JSON编码使用UTF-8、无额外空格；步骤保留原编号。同名原轨迹不会因补采或跨批次而合并。

Excel保留原有业务列；JSON中的来源身份字段不会加到业务表头。图片和XML保存相对该批次原始根的真实路径，资源API使用批次及当前内部修订号定位，不按全局任务名猜路径。

CLI默认工作文件保存在临时目录，完成后清理；当前结果仅保存在批次阶段目录。显式 `--export-output / --annotated-output` 仍尊重调用方指定位置：

```powershell
python backend/trajectories_preprocessing.py --source backend_workspace/raw/rollout_trajectories --batch-id my-processing-batch
```

## 作业API

| 接口 | 行为 |
| --- | --- |
| GET /api/trajectory-preprocessing/batches | 返回批次摘要、就绪数量、当前02内部修订号、最新作业、产物和不可启动原因 |
| POST /api/trajectory-preprocessing/jobs | 请求 `{"batch_id":"..."}`，返回作业，HTTP202 |
| GET /api/trajectory-preprocessing/jobs?batch_id=... | 查询作业历史 |
| GET /api/trajectory-preprocessing/jobs/{job_id} | 恢复状态、当前阶段、真实步骤计数和错误 |
| POST /api/trajectory-preprocessing/jobs/{job_id}/retry | 使用原冻结输入重试，HTTP202 |

作业状态：`queued/running/succeeded/failed/interrupted`；执行阶段：`scanning/converting/annotating/publishing`。阶段进度使用 `completed_steps/total_steps`，附当前任务、原轨迹名和步骤号；发布完毕才将作业置为完成。扫描尚未得出总数时显示不确定进度。所有新增API响应禁用缓存。

`artifacts` 返回已登记的完整阶段清单，文件下载使用已有：

```text
GET /api/data-batches/<batch_id>/artifacts/<stage>/files/<filename>
```

不存在的批次／作业返回404；没有可用轨迹、清单冲突、文件校验失败、不可重试或配置变化返回409；请求结构错误返回422。

## 重复操作、恢复与并发编辑

- 同批次正在执行时重复启动返回现有作业。相同输入与处理配置已有成功结果时直接返回它，不重复调用模型。
- 首次标框失败后重试保留转换结果；已有成功链时，新01/02计算成功后一起替换，失败保留原链，复用相同批次和配置下有效的模型缓存；新采集不会混进旧作业。
- 重启把执行中作业标记为中断，用户主动重试。代码／模型配置变化时要求重新开始，避免混用不同规则。
- 同批次已有较新输入的成功结果时拒绝重试旧失败输入，防止当前结果倒退；相同输入已被成功重试则返回该成功作业。
- 新02发布前检查启动时的基准版本。预处理期间发生人工改框时保留人工版本并报告冲突；重试不能绕过这一检查，重新开始才采用新的基准。
- 轨迹读取与图片请求携带 `batch_id、annotation_version`；改框携带同样信息并校验预期版本，成功返回新版本。建树请求使用 {batch_id, task_ids}，内部冻结所选任务的输入指纹。页面不选择标框版本。
- 建树提交、实际执行和发布前验证新采集输入清单及原文件，防止采集完成后图片被替换。已有validation没有该采集清单，维持既有产物校验，不伪造来源。
- 当前应用按单进程后端和共享后台队列部署。前端切换批次清理旧缓存与轮询，拒收晚回包，并保护未保存标框。

## 验证与接入边界

### 标框固定步骤失败的诊断与纠正（2026-11-12）

采集端的 `stepXXX_vla_model_response.json` 是采集模型响应；平台标框模型的响应由本模块另行保存。有效响应仍进入 `qwen_review_cache.json`。失败响应、原始 bbox、候选像素框、截图尺寸、真实执行动作、实际传入的 `action_summary` 以及转换前后的框，写入该缓存同级的 `box_failures/<请求摘要前20位>.json`。缓存可能使用短路径映射，因此应在实际数据根的 `cache` 下定位，不假定固定长目录。

诊断仅保存脱敏文字与来源引用，不复制截图；每个请求保留最近最多8次失败，单文件不超过128 KiB，同请求重试覆盖同一文件。超长原始响应标记截断并保留原始文本摘要；纠正成功后标记 `resolved=true`。错误消息包含诊断路径；托管自动流程的公开失败摘要仍使用固定脱敏文案，可在平台数据根查找该诊断文件。

审核提示分别标明执行像素坐标和输出0～1000坐标，并提供同一执行动作在输出坐标系中的位置。无效 JSON、截断、越界、过窄框及不包含实际 click/long_press 点或 swipe 两端点的框，要求模型根据同一截图纠正。不会通过猜测单位、挪动点击点或随意扩框让无效结果通过；旧缓存也执行相同校验。

普通预处理每个审核轮最多请求3次（首次＋2次纠正）；托管自动流程每轮仅请求一次，交由已有作业重试策略控制总次数，下次同输入请求读取上次安全失败原因作为纠正提示。纠偏/COT 中的标框同样遵循此约定。网络异常不进入输出纠正循环；图片、缓存或诊断写盘错误不冒充模型输出错误。

更新代码会改变预处理配置指纹。原配置下的“重试预处理”仍复用原冻结输入；代码升级后应通过“开始新预处理”采用新规则，沿用原采集数据，无须重新采集。自动流程继续遵守原有配置变化和作业重建规则。本轮未改变 XML 画布推算方式；个别候选框来源问题需结合失败记录继续确认。

```powershell
python -m unittest backend.tests.test_preprocessing_jobs backend.tests.test_preprocessing_downstream_integration -v
python -m backend.tests.collection_run_demo
npm test --prefix frontend
npm run build --prefix frontend
```

测试使用临时数据和模拟模型／采集服务，不向真实手机下发。采集服务源码不在本仓库：这里只实现下发参数、接收完成清单、预处理及页面。采集端必须实际执行目录写入和完成登记；没有登记时页面不会把下发回执当成轨迹就绪。


分次建树和质检会按任务合并至当前过程件；改框只失效对应任务。修正、COT、发布均使用同一业务批次。详细规则和迁移命令见 [DATA_STORAGE.md](../DATA_STORAGE.md)。


发布成功后，业务批次持久转为 published，从预处理及其他工作列表移除。已结束批次不能再次采集、预处理、建树、质检、修正或导出，迟到结果也不能回写；409 响应包含发布编号。后端保留原始采集文件和每阶段唯一过程件供只读查看、下载。新生产使用新的 batch_id；发布失败时保留原状态和前端工作内容。既有发布补登记步骤见 [存储规则](../DATA_STORAGE.md#发布后结束批次)。
