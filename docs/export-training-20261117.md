# 多轨迹完整导出、OPD 与训练 JSONL（2026-11-17）

本次修改平台后端、自用前端和单独交付的采集端路径适配。创新平台前端按下述接口适配，不覆盖其 API_BASE_URL、路由或组件体系。没有改变模型配置、手机任务分配、App 复位、评分公式和自动 Top-1 阈值。

## 使用路径

1. 纠偏页面从质检合格候选建立会话。默认选 Top-1；同任务可增选其他轨迹。修改、复核和选择按任务＋轨迹身份独立保存，切换轨迹或刷新保留。
2. 需要 COT 时沿现有 COT 操作生成，再使用“导出已选轨迹”；也可直接选择 OPD 导出。OPD 不提交 COT 或标框模型作业，仅在导出副本清空 thought。
3. 新完整选轨导出包含每条所选轨迹的全部未删除步骤。旧 SFT/RL 截至修正点的分流导出仍为原规则，两者不能混称。
4. 在纠偏完整选轨／OPD 导出时，根据最终表格及图片前缀生成配套 JSONL，可立即下载。发布保存同一组表格和训练文件，上传读取该版本，不读取后来页面勾选。
5. 发布页支持独立填写 JSONL 目标目录并单独上传，显示转换样本数、排除数、目录进度及报告。没有有效样本时不把空 JSONL 当成功，表格／原件上传结果独立保留。详细字段对应见 [Excel 配套 JSONL](paired-training-jsonl-20261117.md)。

自动流程人工模式冻结 `export_profile`、`cot_policy` 和选轨集合；OPD 的 COT 阶段显示跳过。全自动继续原 Top-1／既有 thought 策略，旧流程缺省不变。

## 新旧物理目录

- 平台在 `collection_storage_layouts`、`collection_evidence_locations` 登记完整身份到短目录的映射。新原件通常为 `raw/collection_batches/<batch>/runs/<r短号>/<tr短号>/<原文件>`。
- 采集端新运行登记为 `.http_runs/r00000001/` 等独立短目录；worker、实例及产物也采用持久短别名。完整运行编号仍在规范记录、请求、清单和 API 中。
- 目录布局是存储元数据，不进入旧请求指纹；旧记录的 `result_dir` 及已登记映射优先。新续采段可用短布局，旧段保持不动。
- 运输 ZIP、清单及其摘要保持原件；本地映射单独登记。读取、回传重试、预处理、坐标、纠偏、发布、独立评测和清理使用实际登记路径。
- 新导出采用固定短物理文件名 `data_<e短号>.xlsx`；说明留在页面和元数据。
- 受控标准新路径以 240 个 UTF-16 单位为预算目标；现场常规数据根的代表性平台路径约 124～162。任意超长根目录、外部文件名、深层 skill 不能保证小于 240，保留 Windows 扩展路径 I/O。不增加因长度而卡住历史读取的门禁，不搬迁根目录或旧数据。

## 创新前端接口适配

继续使用现有接口和项目统一 API 地址：

- `PATCH /api/correction/sessions/{session}/tasks/{group}/export`：按 group 保存选择，携带 `export` 和 `expected_revision`。不要在前端把同任务的选择压成单个 trajectory。
- `POST /api/correction/sessions/{session}/dataset-export`：新流程发送 `export_profile: "corrected_selection_v1"`、`cot_policy: "generate" | "skip"`、`expected_revision`，可填写完整 URI `training_image_prefix`。响应含配套文件 `training`。不传新 profile 仍走旧完整导出。
- 自动流程创建请求增加同名 `export_profile`／`cot_policy`；只在明确 OPD 时发送 `skip`。
- `POST /api/dataset-releases/{release}/upload`：保留可选 `training_jsonl: true`，增加 `training_only` 和独立的 `training_destination_prefix`。配套文件可仅上传 JSONL，不必重传 Excel／图片；旧发布的上传阶段转换仍需 `include_trajectories:true`。继续使用 `target:"internal"`、已有 `destination_prefix` 和重试字段。
- 上传作业 GET 返回训练转换状态、数量、摘要及错误；沿既有 GET 使用 `?download=jsonl` 或 `?download=report` 下载，避免额外依赖网关新增路径。兼容的 `/training/{kind}` 别名也可用。
- 列表统计分别显示任务数、轨迹数、实际导出步骤数。标记按具名字段读取，不能以 Excel 最后一列位置推断。

页面刷新沿用现有摘要／详情查询，不清空已选集合、不用未冻结默认值替换正在执行的策略。

## Excel 与 JSONL 契约

新增 Excel 列 `is_key_step`、`is_refined`；完整选轨导出还补齐任务正文、任务／轨迹编号、步骤与当前历史及训练图片映射，供纯表格转换。JSONL 同名两个顶层标记字段，既有 `messages`、`solution`、`images` 保留。system/user 模板与附件保持一致，solution 顺序为 thought → tool_call → action → sftcall → uuid → metaID。

`is_key_step` 使用冻结原树的 occurrence 和稳定步骤身份。同父节点至少两个有效 Q 分支且未舍入差值大于 `1e-8` 时标记对应子分支动作；等分 false，无法判定 null。原 Q、覆盖和版本留在导出元数据，不把人工修改后轨迹当作已重新评分。

`is_refined` 只统计实际生效的人工动作或 SOP 修改，同一步两者都改只计一次；恢复原值、JSON 排版变化、模型生成及 OPD thought 清空不算人工修正。旧分流导出的前缀标签语义不变。

转换按发布来源＋任务＋轨迹分组，而非相邻任务文本。history 为该轨迹前序保留步骤；当前步骤和未来步骤不进入。移除附件按“最后一列空白删除整组”和固定 UUID 池的规则；UUID 由冻结发布、来源、步骤、格式版本确定，重试稳定。

tool_call 优先使用与当前动作和图片匹配的有效框；动作修改使旧框失效时采用当前动作 JSON。打字、等待等直接使用动作 JSON。SOP 优先人工值，其次当前有效摘要，否则生成确定性动作描述，不调用模型。保留现有坐标值，不隐式做像素／归一化转换。

图片通过真实冻结上传目录映射生成 `s3://<bucket>/<object-key>`，保留目录内相对层级。缺图片或无法解析动作逐样本报告；缺 XML、Q、thought、bbox 不排除。训练样本被排除不等于删除执行步骤，能读到的动作仍进入后续 history；不能构成必要历史时说明后续样本排除原因。

## 上传与数据保护

新导出每份 Excel 配套一份 JSONL，发布保留各自配对关系，多表上传用序号子目录隔离。训练文件的目标目录可单独修改，变化创建新上传任务，不改变已生成的图片引用或样本编号。只生成小型 JSONL 与排除报告，不复制轨迹、不生成中间 Excel、不打 ZIP、不改原件。综合上传仍先传图片再传训练文件，也可仅上传配套 JSONL；重试复用已确认成功的目录。旧发布没有配套文件时继续使用旧版上传阶段合并转换。

原上传脚本的 Python、token、region、bucket 配置不变。私有脚本须支持现有目录参数并在真实上传完成后退出；本地模拟上传不能证明内网云道已成功。

## 验证与部署

定向测试覆盖两引擎短路径封存与读取、旧路径读取、续采／清理、Windows 长路径 I/O；多选完整导出、OPD 无模型调用、Q 分叉标记、修正隔离、模板、history、S3 路径、样本排除、重复提交及失败重试。具体命令、计数及跳过项见交付 `VALIDATION.md`。

先在安全窗口更新实际采集端及其 Pipeline runtime，核验 `storage_layouts` 与 `COMPACT_STORAGE_VERSION`；再同步平台。不要热覆盖活动采集源码，不删除或搬迁旧数据。创新前端按接口增量适配。

内网待验：两引擎各小规模新运行＋旧目录读取，现有任务“10 选 2”完整导出并上传，核对私有脚本和训练读取端的 S3 URI、动作及两个标记。未执行真实手机／私有上传／训练消费验收，不能标记为现场通过。
