# 批次与当前过程件

业务批次统一使用 `batch_id`。一个批次可以分次处理不同任务，但每个阶段只有一份当前过程件；先处理 A 再处理 B，结果合并为 A+B。前端不提供标框版本或建树运行选择器。

默认数据根为项目根的 backend_workspace/，启动前可用 ADF_DATA_ROOT 指定其他目录。后端不扫描 backend/_workspace/、frontend/data/ 或迁移备份补数据。

## 存储职责

| 形式 | 内容 |
| --- | --- |
| SQLite：system/app.sqlite | 当前索引、作业进度、人工草稿、稳定步骤身份、任务失效状态和发布登记 |
| JSON | 当前阶段的完整有效结果，供下游读取 |
| Excel | 与 JSON 同次保存的完整核对表，供查看和下载；修改下载文件不会回写业务数据 |
| 发布目录 | 独立冻结的交付表、来源 JSON 和校验清单 |
| 迁移备份 | 批次目录之外的迁移前完整数据，只用于审计和回滚 |

缺失或损坏的必需 JSON 明确报错，不回退 Excel，也不自动调用模型补齐。仍通过业务上传入口接受的知识库、失败用例和采集 Excel 不受影响。

~~~text
backend_workspace/
├─ system/
│  ├─ app.sqlite
│  ├─ batch_locks/                  批次跨进程锁
│  ├─ preprocessing/                冻结输入清单；完成后清理计算文件
│  ├─ task_generation/              作业输入及采集表
│  └─ trajectory_correction/        当前活动会话输入及当前导出
├─ raw/                             原始轨迹、截图、XML和采集原件
├─ inputs/phone_factory/
├─ resources/task_generation/KnowledgeBase/
├─ batches/<batch_id>/<stage>/
│  ├─ result.json                   当前完整结果
│  ├─ result.xlsx                   名称以manifest为准，建树主要保存JSON
│  └─ manifest.json                 内部修订号、摘要、来源和文件SHA256
├─ releases/<release_id>/
│  ├─ manifest.json
│  ├─ 001/<导出文件>.xlsx
│  └─ provenance/                   冻结来源JSON、最终表和索引
├─ cache/
├─ logs/
└─ tmp/artifact_transactions/       文件提交恢复日志，完成后清理
~~~

batches/ 下不再有时间戳版本子目录。manifest 中的 version/revision 是内部并发校验令牌，不能作为用户选择历史结果的入口。作业编号仍用于查询进度和日志。

## 阶段

| 目录 | 当前产物 |
| --- | --- |
| 00_task_generation | 生成结果 JSON/Excel |
| 00_scene_matching | 扩增来源、分类和错误 JSON/Excel |
| 00_augmentation | 扩增结果 JSON/Excel |
| 00_collection | 采集输入 JSON 和原 17 列采集表 |
| 00_task_export | 当前未删除任务 JSON/Excel |
| 01_conversion | 转换后的步骤 JSON/Excel |
| 02_annotation | 步骤、动作框 JSON/Excel |
| 03_observation | 全部有效任务步骤的 Observation、中间态判断及未入树原因 |
| 04_tree | 有效任务的轨迹树、来源标注和完整质检输入 JSON |
| 05_quality | 有效任务评分 JSON、轨迹汇总/步骤评分/评分标准 Excel |
| 06_correction | 有效任务完整修正过程 JSON/Excel及当前筛选导出 |
| 07_cot | 有效任务完整 COT 过程 JSON/Excel及当前完整数据集导出 |

过程表保留有效任务中已删除或未入树的步骤及对应标记；训练导出遵循删除和导出选择规则。待复核或失效任务不混入有效导出。

## 更新、依赖与恢复

- 相同任务、输入和配置的重复提交复用正在执行或已成功结果。失败允许重试，已有有效结果不会因计算失败被替换。
- 建树和质检按任务合并。新增 B 不改变 A 的输入指纹，也不会使 A 的计算或编辑无故失效。
- 修改 A 的标框只让 A 的建树及后续结果失效。页面显示待重新处理；B 保持有效，不自动调用模型。
- 每批次维护一个活动修正会话。编辑、导出选择和 COT 按稳定的任务／轨迹／步骤身份关联；Excel 行号只用于展示。
- 来源未变的编辑保留；来源变化的编辑保留为待复核，采用或放弃后才允许该任务参与导出、COT和发布。
- 编辑请求携带内部修订号。冲突返回409，页面保留草稿并要求刷新。
- 后台回写前校验所选任务输入指纹，拒绝旧结果覆盖新输入。
- 文件先写暂存目录，持有批次锁后替换并以数据库事务登记；03/04成对提交。恢复日志处理文件替换或数据库提交中断；任务失效通知通过事务记录恢复。
- 后端仍按一个 worker 部署。跨进程文件锁和 CAS 防止存储冲突，不代表引入分布式模型作业调度。

首次预处理可以先发布01，保留标框失败时的转换结果；已有01/02成功链时，新计算完成后一起更新，失败保留原链。原始采集文件和作业冻结输入清单继续保留。

## 当前结果接口

| 接口 | 行为 |
| --- | --- |
| GET /api/data-storage | 实际数据根、数据库及当前阶段保存规则 |
| GET /api/data-batches | 业务批次及阶段数量 |
| GET /api/data-batches/{batch_id}/artifacts | 各阶段当前清单 |
| GET /api/data-batches/{batch_id}/artifacts/{stage} | 当前阶段详情 |
| GET /api/data-batches/{batch_id}/artifacts/{stage}/files/{filename} | 校验并下载当前文件 |
| GET /api/data-batches/{batch_id}/tree | 当前树和全部任务状态 |
| GET /api/data-batches/{batch_id}/quality | 当前质检和全部任务状态 |
| GET /api/data-batches/{batch_id}/tasks/{task_id}/tree | 单任务有效树 |
| GET /api/data-batches/{batch_id}/tasks/{task_id}/quality | 单任务有效质检 |

建树、质检提交使用 {batch_id, task_ids}；修正会话用 {batch_id} 幂等获取或创建。所有页面路由定位使用 batch_id。

兼容旧链接时，只有仍对应当前有效结果的运行标识才映射到业务批次；已淘汰的版本返回明确失效响应（410），不静默替换成另一份结果。内部资源访问仍允许携带当前修订令牌用于一致性校验。

修正 /export、/dataset-export 和发布下载沿用业务接口，不能把过程核对表当作训练导出。当前会话仅保留每种导出的当前文件，已发布文件独立保存。

## 发布

发布时冻结所选完整数据集 Excel、来源 JSON、上游关系和校验清单。训练总览统计读取发布内的冻结来源，不再依赖随批次更新的当前文件。发布成功在同一数据库事务中登记发布、更新修正会话并将所有所选批次置为 published。结束后不提供恢复编辑入口；原始文件、过程件和已保存人工修改只读保留，下载、SHA256、统计和 S3 上传继续可用。后续生产使用新的 batch_id。

## 迁移为每阶段一份

先停止后端、模型作业和所有写入脚本。从项目根执行：

~~~powershell
python -m backend.data_store.migrate_single_artifact check
python -m backend.data_store.migrate_single_artifact apply
python -m backend.data_store.migrate_single_artifact verify
# 验证不通过且没有迁移后业务写入时，可恢复迁移前数据
python -m backend.data_store.migrate_single_artifact rollback
~~~

命令接受 --data-root <目录>。备份和校验清单位于数据根同级 .single-artifact-migration/<数据根目录名>/，不放在 batches/ 内。apply先完整备份数据库及文件并验证SHA256，再冻结已有发布来源、沿上游关系选择完整有效链、重建索引和稳定身份，最后删除运行目录中的旧版本和重复计算文件。

迁移不会分别挑选每个阶段最新文件拼接。现有 validation 批次保留9月16日建树和匹配的质检、修正、COT链；旧空修正会话只留在备份。人工编辑、COT和原始采集原件保留。若发现无法自动合并的多个人工草稿、缺失来源或校验失败，迁移明确报错。

verify检查阶段结构、文件摘要、上游引用、人工编辑和发布统计。回滚先验证备份；迁移后有新业务写入时拒绝直接覆盖。失败迁移应执行verify调查或rollback，不手动混合新旧数据。备份长期保留，由操作者另行归档。

## 原数据根更名

已有 data/ 到 backend_workspace/ 的根目录迁移工具仍独立可用：

~~~powershell
python -m backend.data_store.migrate_root check --project-root .
python -m backend.data_store.migrate_root apply --project-root .
python -m backend.data_store.migrate_root verify --project-root .
python -m backend.data_store.migrate_root finalize --project-root .
# finalize之前且无新增业务写入时可rollback
~~~

这一步不能代替阶段迁移。历史JSON中的旧绝对路径由已登记根目录映射解析，原始文件字节不改写。

## 验证

~~~powershell
python -m unittest discover -s backend/tests -q
npm test --prefix frontend
npm run build --prefix frontend
~~~

测试使用临时数据、模拟模型和模拟采集；覆盖任务合并、局部失效、并发提交、迟到回写、跨进程CAS、进程中断恢复、编辑稳定身份、发布冻结和迁移回滚。真实模型或手机采集需用户在业务页面主动发起。

更多采集流程见 [批次预处理](backend/PREPROCESSING.md) 和 [采集完成协议](backend/COLLECTION_RUNS.md)。


## 发布后结束批次

生命周期保存在 SQLite 的 batch_lifecycle 记录中：缺省 active，成功发布后为 published，携带 published_at 和 release_id。发布响应包含 release.batch_ids；GET /api/data-batches/{batch_id}/lifecycle 返回持久状态。批次锁覆盖完整性检查、冻结文件和数据库提交，多批次同时成功或全部回滚。

整批的全部任务须有当前有效的预处理、建树和完整质检结果，且没有执行中作业、失效任务和待复核修改。保留原有轨迹筛选规则，不要求所有步骤生成 COT。失败不改变批次状态。已结束批次的处理入口及写入返回结构化 409（batch_published、批次和发布编号）；各阶段只读详情、下载以及作业日志仍保留。

前端的采集、预处理、质检、修正、COT 和发布候选只列 active 批次。发布后通过同窗口通知和 BroadcastChannel 移除对应批次，失效请求和轮询；重新聚焦/恢复可见时核对服务端。当前选择清空并显示已发布，不自动切到下一批。不调用 localStorage.clear，发布历史和 S3 上传恢复信息保留。

### 为既有发布登记状态

在停止写入后执行（不调用采集或模型）：

    .venv/Scripts/python.exe -m backend.data_store.migrate_batch_lifecycle check
    .venv/Scripts/python.exe -m backend.data_store.migrate_batch_lifecycle apply

只有来源会话仍与最后一次发布内容指纹相同、整批完整的批次才会登记结束。旧发布后有修改、未完成任务或待复核内容会保留 active，并在 skipped 中列出原因。apply 先用 SQLite 在线备份保存数据库，执行 integrity_check 和 SHA256 校验，再在单个事务中补登记生命周期。备份位于数据根同级 .batch-publication-migration/<根目录名>/<迁移编号>/app.sqlite；report.json 保存判断原因、全部 batches/releases/raw 文件校验清单和验证结果。过程文件、发布文件和原始文件不改动。重复执行不会再次登记。

若需撤销这次补登记，停止后端和所有写入后，用此次 app.sqlite 备份恢复 system/app.sqlite，再重启；这会恢复备份时点的全部数据库状态，因此恢复前也应保存当前数据库。
