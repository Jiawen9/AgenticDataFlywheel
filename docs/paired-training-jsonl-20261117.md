# Excel 配套 JSONL 与独立上传

本次仅修改平台后端、自用前端和说明。无需同步采集端，不修改评分、模型、手机采集或原始轨迹。不新增中间 Excel，不复制图片，不调用模型。

## 使用方式

1. 纠偏中选择轨迹，填写图片前缀（例如 `s3://bucket-jinglun/data/images`）；默认采用后端已有 `ADF_YUNDAO_BUCKET`／`ADF_YUNDAO_PREFIX`。本地转换不要求上传 token 或私有脚本可用。
2. 使用“导出已选轨迹”或 OPD 导出，获得 Excel、配套 JSONL 和排除报告。一份 Excel 对应一份 JSONL；每个可转换动作步骤是一行样本。OPD 的 thought 内容为空。
3. 数据发布保存该组文件。发布详情可下载 JSONL／报告，也可只上传 JSONL，单独填写它的云道目标目录。
4. 上传图片沿用导出表中登记的图片前缀与相对路径。改变 JSONL 文件的上传目录不修改它内部的图片地址。图片尚未上传不阻止单传 JSONL；传输完成不代表训练端已经能读取所有图片。

没有图片前缀配置时，Excel 导出仍然可用，页面明确提示配套 JSONL 待配置；不能把本地绝对路径伪装成云道地址。补齐前缀后重新导出即可。旧 SFT／RL 分流导出保持原有语义；历史发布不批量改写。

## 表格到样本的字段对应

| JSONL 内容 | 最终导出表／固定规则 |
| --- | --- |
| `messages[0]` | 原脚本固定 system prompt |
| user 的 `Task` | `task` 真实任务文本，不用 `meta_task` 轨迹编号替代 |
| user 的 `Execution History` | 按任务、轨迹和步骤顺序，使用本轨迹前面的保留步骤；首步 `Empty` |
| user 的截图占位符 | 固定 `<image>`，图片文件不嵌入 JSONL |
| `<thought>` | `thought`；OPD 为空 |
| `<tool_call>` | 当前有效 `actions_box`；缺框或旧框失效使用当前 action JSON |
| `<action>` | 当前有效 SOP／摘要／确定性动作描述 |
| `sftcall:` | `action` 或 `actions` 中的当前动作 JSON，不重新缩放坐标 |
| `uuid` | 同一导出内容与稳定步骤身份生成，上传重试不改变 |
| `metaID` | 轨迹组序号与步序号，例如 `1.0`、`1.1`、`2.0` |
| `images` | `training_image_prefix` 加 `training_image_relative`，保留嵌套图片路径 |
| `is_key_step`／`is_refined` | 导出表中的原树关键步标记／实际人工动作或 SOP 修正标记 |

新表补齐 `task`、`task_id`、`trajectory_id`、`step`、`history` 和训练图片路径字段，保留原 `image`。导出时完成当前动作、SOP 和框的适配；转换器从 Excel 读取，不再为补任务信息扫描原轨迹或查询采集端。

不沿用原脚本“最后一列为空就删除整组”的规则，也没有 120000 个预分配 UUID 的上限。缺任务、动作不可解析或图片路径无法映射时列明排除项；其他轨迹继续转换，Excel 仍保留原步骤。缺 XML、Q、thought 或 bbox 不是排除条件。前序动作不可解析时，受影响的后续历史明确报告，不能悄悄缩短历史。零可用样本标记为空，不将空文件上传当作有效训练数据成功。

## 接口适配

- 纠偏 `POST /api/correction/sessions/{id}/dataset-export`：保留 `expected_revision`、`export_profile`、`cot_policy`；增加可选完整 URI `training_image_prefix`。响应 `training` 返回版本、状态、样本数、排除数、图片前缀、文件摘要和下载地址。旧客户端不传新字段仍可导出 Excel。
- 发布详情增加 `training_files`，一份导出对应一项。JSONL／报告下载：`GET /api/dataset-releases/{id}/training/{index}?kind=jsonl|report`。
- 上传保留 `training_jsonl`，增加 `training_only` 与 `training_destination_prefix`。后者是 bucket 内 JSONL 目标目录，不是图片前缀；单传不要求重新上传 Excel 或轨迹。
- 配套能力为 `internal.training_pair_version=2`；旧 `training_jsonl_version` 与旧上传任务兼容。旧发布没有配套文件时保留原上传阶段转换逻辑。
- 新增文件与修改文件一起合入；创新平台沿用自身 `API_BASE_URL`、网关前缀、路由和组件体系，不复制替换全项目。

## 数据与验证边界

发布只复制小型表格、JSONL 和报告，原截图目录不复制。JSONL 上传目录只包含训练文件，排除报告单独供下载。源文件和发布副本校验摘要；不同上传范围、图片映射及训练目标参与相应请求去重，重试不重新选择轨迹。

定向验证覆盖完整选轨／OPD、相同任务多轨迹隔离、当前／未来步骤不泄漏进历史、当前动作与旧框处理、中文路径、配套发布、单传、目标变更、重复请求、失败重试及旧上传兼容。结果与命令记录在本次交付验证中。未执行真实云道私有脚本、内网网关或训练加载器验收；本地模拟不能替代现场通过。

### 本次验证结果

- 后端 144 项：首次 143 项通过，1 项真实本地模拟上传脚本测试因测试解释器缺少既有 `psutil>=5.9,<8` 依赖而未启动。在临时测试目录安装 psutil 7.2.2 后，该项单独复验通过。正式 Python 环境与依赖清单未修改，无跳过项。
- 新增 20 项定向用例包含真实 Excel 读回、实际纠偏导出到发布及模拟上传、逐样本图片路径对应、真实 HTTP 下载、前缀冻结、嵌套截图完整目录、部分文件失败恢复及成功回执复用。
- 前端 5 个测试文件共 54 项通过；应用与 Vite 配置类型检查通过；Vite 构建通过。已有第三方注释及大 chunk 提醒仍存在。
- TestClient 需要本机 socketpair；沙箱限制导致的挂起通过允许本机测试执行解决，没有修改服务代码或框架依赖来绕过测试。

后端相关模块：`test_paired_training_export`、`test_paired_training_upload`、`test_corrected_selection_export`、`test_export_markers`、`test_pipeline_release`、`test_data_publishing`、`test_directory_upload_jobs`、`test_upload_sources`、`test_training_jsonl_upload`、`test_internal_dataset_upload`、`test_correction_publishing_pages`、`test_correction_text_edits`、`test_trajectory_correction`（均位于 `backend.tests`）。

前端命令：

```powershell
npm test -- src/composables/useCorrectionWorkspace.test.ts src/views/CorrectionReadPages.test.ts src/views/DataPublishingUpload.test.ts src/components/DatasetFolderUploadProgress.test.ts src/datasetPublishingApi.test.ts
npm run build
```

### 提交与同步

提交标题：`feat: 导出 Excel 配套生成 JSONL 并支持云道独立上传`

摘要：补齐导出表的任务、轨迹、步骤和有效历史字段，按原脚本模板生成配套 JSONL；冻结发布保存表格与训练文件，支持独立目标目录、部分失败重试及配套下载。旧导出和旧上传路径兼容，采集服务不变。

同步平台后端及自用前端，包含新增转换模块和测试；不要只复制已有同名文件。使用既有云道后端配置。创新前端按上述字段与展示规则适配。代码提交、部署及真实云道验收由用户后续执行，本次没有代为推送或重启服务。
