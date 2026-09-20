# 外部完整步骤表上传与发布

## 页面流程

在“数据发布”点击“上传外部表格”，填写发布名称，选择一个 .xlsx 或 .xlsm 文件（最多 50 MiB）。来源默认“人工采集”，数据日期默认今天，可改为历史日期。App 和两级场景为可选补充值，表内分类优先；仍缺失时归入“未记录 App／未分类”。

点击“校验预览”后可查看工作表、轨迹数、步骤数、分类分布、错误和警告。默认活动工作表，多工作表时可以切换；换表、换文件或修改统计元信息后必须重新校验。发布名称不影响统计预览。

校验通过后点击“创建并发布”，自动打开发布详情；只清理本次上传草稿，保留平台批次的选择和编辑状态。转换在后台自动执行，成功后累计到完整汇总表及场景／App 看板。历史日期数据可通过看板日期筛选查看。转换失败可在发布详情重试，已发布原件仍能下载和上传云道S3。

## 表格约定

接收与平台“完整数据集”一致的**逐步骤明细表**，不是 all_data.xlsx 轨迹汇总表。首行是表头，每个非空数据行是一条步骤：

- 必需轨迹身份列：trajectory_id，或文件夹名／meta_task／轨迹。后几种身份同时结合 image 的父路径，沿用完整表关联规则。图片路径仅作为文本身份，不访问图片或外部文件。
- 必需动作列：actions 或 action。沿用现有动作解析；不能解析的步骤仍计入 unknown，并在预览中定位提示。
- 分类列支持 APP／app／涉及APP、一级场景／scene、二级场景／capability。同一轨迹各步骤的非空分类必须一致，空白步骤继承该轨迹已有分类，整条轨迹缺失时才使用表单补充。
- 空表、重复表头、缺少必要列或轨迹身份、分类冲突阻止发布，错误包含工作表、行和字段。
- 每份文件只发布一个选定工作表。拒绝加密或损坏的工作簿，解压内容最大 512 MiB。
- 全表使用填写的数据日期及来源统计，不从逐行日期或来源覆盖它们。真实发布时间单独保存。
- 外部采集不推断人工精修，精修记录保持未知；看板继续按 DEV 规则，仅有“数据飞轮”来源时显示精修卡。

## 接口

### POST /api/dataset-release-imports/preview

multipart/form-data 字段：file、data_source、data_date（YYYY-MM-DD）、app、level1、level2、sheet_name。sheet_name 省略时选活动表。

响应包含 import_id、valid、sheets、sheet_name、filename、expires_at、summary、errors、warnings、duplicate_release。summary 包含 trajectory_count、step_count、apps 和 scenes。问题为 {sheet, row, field, message}。业务校验失败返回 200 和 valid=false；文件类型错误 422，超过上传限制 413。

### POST /api/dataset-releases/import

JSON：{import_id, name, request_id}。request_id 由客户端为一次提交生成；网络中断后使用同一编号和参数重试。返回 {release}，包括 source_kind="external_manual"、external_import 和 batch_ids=["external_…"]。历史平台发布的来源类型默认为 workflow。

相同文件及工作表重复提交，或规范化内容与统计参数完全相同的不同文件，均返回已有发布，发布名称不能用来重复计数。相同原件及工作表的元信息与已发布记录冲突时返回 409 和原 release_id。不同业务工作表可独立发布；规范化内容完全相同仍复用原发布。

预览过期返回 410/import_expired；暂存发生变化返回 409/import_changed；元信息冲突返回 409/duplicate_metadata。以上情况保留表单并要求重新预览。同一 request_id 携带不同参数返回 409/idempotency_conflict。

## 保存与恢复

新批次直接登记 published；不创建采集任务、纠偏会话、00–07 阶段过程件，不进入处理工作列表。平台批次的发布完成条件保持原样。

默认数据根 backend_workspace 中保存：

    releases/<release_id>/
      001/<上传原始文件名>
      external-data.json
      import-manifest.json
      manifest.json

原 Excel 保持上传时的原字节。规范化 JSON 保存按轨迹汇总后的统计来源、元信息和解析版本；导入清单保存原件／JSON 的 SHA256、选定工作表、规范化内容摘要及来源关联。转换读取并校验这些冻结来源，不查找纠偏会话；不同批次的同名轨迹分别计数。下载与云道S3复用原件，不上传临时解析产物。

SQLite 在一个事务中登记发布、批次关闭、导入状态、幂等请求和文件／内容索引。跨进程导入锁防止并发重复登记；文件先暂存并冻结，恢复日志在文件替换与事务之间记录状态。发布登记失败不产生有效发布；进程中断后启动恢复清理未登记产物，允许重试。若数据库提交成功但响应丢失，不能删除已登记文件。

未发布预览保留 24 小时，启动、下一次预览及每小时清理过期暂存。发布转换沿用现有队列与逐发布替换汇总策略，失败保留发布，重试不重复累计。备份需包含 system/app.sqlite、releases 及汇总目录；无需迁移已有发布或修改原过程件。

## 模拟验证

后端覆盖完整 HTTP 发布和真实本地汇总、并发去重、提交中断恢复、分类与身份校验、历史日期、混合来源、精修显隐、原件下载、模拟 S3 上传以及转换失败重试。前端覆盖表单失效、请求取消、网络重试和发布成功后的草稿边界。浏览器脚本为 frontend/tests/external-dataset-import.browser.cjs，全部 API 由模拟数据响应，不访问真实采集、模型或上传服务。
