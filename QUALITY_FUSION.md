# 轨迹质检：Checklist 融合与树节点 Q

最近更新：2026-11-13。轨迹质检由平台后端的 `DevelopRubrics` 与 `ChecklistEvaluator` 执行；新质检不再请求采集端能力、目录或评测服务。模型迭代评估、手机采集、App 复位和采集端源码本轮保持不变。创新平台沿用其 API 基址、路由及组件体系。

## 分数和推荐

同一任务的候选轨迹共用一份冻结 Checklist。迁入原生评测器的 gold 优先、否则生成清单流程；记录真实来源及规则摘要。逐轨迹证据检查及 verdict 仍由现有评测器完成。

- G：原 Rubric 综合分，保留各维度和各步骤评分。
- k/M：已通过条件数 / 冻结条件总数，百分比仅表示条件通过比例。
- F = (k + 0.5 × G / 5) / (M + 0.5)。λ 固定为 0.5；F 不是成功概率。
- 新质检 Top-1 按 F 降序；同分保持原表轨迹顺序。不在 F 前额外按 verdict 分组。
- 自动发布先选新的 Top-1，再检查该条原 G 是否达到原有 0～5 阈值；不回退选择第二名。
- 未检查完、规则身份不一致或无有效 Rubric 证据时，F 为 null，保留已产生的 G、清单和原因，不填零分。其他有效候选仍可展示。
- 历史结果继续使用 G，不自动补算。既有纠偏草稿和人工选中轨迹冻结；评分变化不等于原始步骤变化。真实输入改变仍按原规则失效、复核。

## 树上的 Q

每个动作节点的 Q 是经过该节点的不同轨迹的有效 F 平均值；按 occurrences 的轨迹身份去重，不只统计终点。根节点取全任务有效轨迹的平均值。页面显示 Q（三位小数）和有效评分覆盖数量，未评分显示“待评分”；缺失分数不参与平均。

分支信用 A 使用同级分支 Q 的总体标准差及 epsilon=1e-8，保存在结果中但不在节点显示。缺少任一分支 Q 时 A 暂不可用，单分支或相同 Q 时 A=0。未改变建树匹配分、标框分或既有模型步骤分。

终点仍按轨迹分别展示 Rubric 维度、G、Checklist、verdict 和 F。同一路径完全匹配时可能共享终点，不能把终点平均值冒充单条轨迹分。

## 本地工程与配置

后端新增 `ChecklistEvaluator` 独立工程，只迁入评测、模型、规则和只读证据模块，不引入手机 Agent／ADB。平台已有质检调度管理作业，Checklist 使用隔离 Python 子进程，避免 `agent/config` 导入和环境变量污染 Rubric 或 FastAPI。

- `ADF_CHECKLIST_PYTHON` 指向平台电脑的解释器，未配置时使用平台解释器；安装 `backend/ChecklistEvaluator/requirements.txt`，原 `ADARUBRIC_PYTHON` 不变。
- 在质检页独立配置 text/vision 的 URL、实际模型 ID、密钥环境变量名与本地规则。首次迁移复制原模型迭代默认值，此后互不覆盖；真实密钥只在平台后端环境／backend/.env 中提供。
- 原规则按原 ID/digest 导入 `DATA_ROOT/resources/checklist_profiles`；可用 `ADF_CHECKLIST_PROFILES_DIR` 指定别处。工具 `python -m backend.ChecklistEvaluator.import_profile --source-profiles <实际原规则根> --profile-id <原ID> --expected-digest <原摘要>` 只导入小型规则并验证摘要，不覆盖同编号不同内容，不复制轨迹。
- 同步新增文件和修改文件。在平台质检安全窗口重启后端并同步前端；本轮不交付采集端包，也不要求重启手机采集服务。

原件直接只读，路径适配在内存中进行；不复制或解压整批轨迹。日志、报告、小型清单存于 `DATA_ROOT/system/qc/<短摘要>/`，规则快照存于 `system/qc_profiles/`，逐轨迹状态和全局并发租约复用 `system/app.sqlite`。原 Rubric 检查点仍在 `cache/rubric_outputs/`，正式质检结果仍发布为 05_quality 版本。旧采集端数据保留不动。

默认两个跨进程执行槽。模型／文件计算在 SQLite 短事务外进行；取消、失败和重启核对进程身份与代次。子进程持续排空输出并及时刷新，日志限量脱敏；单次请求和无进展阶段有界监管，无整批固定截止。

质检专用 `quality_only_v1` 保留判定所需视觉事实、证据引用和 verdict，跳过判定完成后仅供报告展示的重复视觉／文本总结。未请求的总结明确标注，不冒充已生成。

## 旧批次恢复与进度

`b261112-004` 只重试原质检阶段。先按旧输入指纹确认未变化，再创建关联的本地执行作业，继承原冻结模型和规则；旧记录、原件和快照不改。Rubric 复用原检查点，不重采、预处理或建树。

原始 collection_case_id 和验收条件随新输入冻结；旧快照从已登记的 source_annotation 行恢复原编号，按 raw_root 解析相对目录，不能用平台短编号猜原始用例。定义身份变化时同任务重新建立 Checklist 并重评，不能混用旧标准。

本地重试只复用通过完整契约验证的结果。缺 verdict 等无效报告在本次重试生成新尝试，旧报告保留；临时 source_error 不进入稳定缓存键，修复一条不让其它有效条全部重评。仍活动的旧质检先停止并对账，只涉及该质检作业，不停止手机。

页面分别显示 Rubric 完成、Checklist 结束／失败及融合有效数，并显示真实模型阶段、角色和耗时。过程结果按输入指纹与执行代次隔离，失败或中断也可查看已有分数；最终提交且完整有效才能推进。不能将 worker 正常退出或 Rubric 完成 95% 当作整体评分成功。

## 接口适配

沿用 `GET/PUT /api/quality-jobs/settings`。GET 增加 execution_backend=platform、evaluator_config、model_configured、本地 profiles；PUT 支持只保存 evaluator_config 或规则 ID/digest，模型为 text/vision 各自的 base_url/model/api_key_env，不接受真实密钥。提交、停止、重试路径不变。任务结果包含：

- `scoring_version: checklist_fusion_v1`、`trajectory_order`、`fusion_order`。
- `evaluations[trajectory_id].checklist`：状态、来源、定义摘要、total/passed/failed/unchecked、verdict 和逐项结果。
- `evaluations[trajectory_id].fusion`：version、lambda、valid、score、invalid_reasons。
- `node_scores[node_id]`：q、advantage、scored_count、total_count、status。
- Top-1 项新增 `fusion_score` 和 `ranking_basis`，原 `global_score` 含义不变。

新前端缺少这些字段时按旧 Rubric 数据展示，不用 G 假造 Q 或 F。树的建树分和质检 Q 分别展示。轮询沿用当前页面选择保护；不要因新响应切走用户正在看的轨迹。

质检摘要和任务详情仍为 `/api/data-batches/{batch}/quality` 与 `/tasks/{task}/quality`，不可绑定到整批 quality=succeeded 后才读取。根据真实作业状态和结果可用标志展示部分结果；查看结果与后续操作权限分开。

### 详情阅读与最高 F 高亮（2026-11-13）

自用前端桌面右栏为 `clamp(380px, 32vw, 480px)`，内部独立滚动；窄屏上下排列。评分区域使用局部深色配色，不修改全站折叠组件。截图保持比例，可打开现有放大预览。

F、Rubric 维度分、Checklist 通过数及结论常驻。Checklist 有多少条就显示多少条摘要，原始描述默认最多两行；点击单项才展开完整描述、理由及证据，同一时间展开一项。同一轨迹的评分刷新保持展开项，切换轨迹或树输入版本后收起。缺项、缺证据及技术错误如实显示，不调用模型生成摘要。

最高分高亮只比较本任务真实终点轨迹的有效原始 F，不按显示小数比较，不拼接各层最大 Q。并列顺序沿用结果 `fusion_order`／`trajectory_order`，旧结果回退树的来源顺序；一次突出一条，可切换并列项或关闭高亮。最高轨迹的根到终点为青绿色，当前选中节点为黄色，节点 Q 不变。部分评分显示“当前已评分最高”和有效覆盖 X/N；无有效 F 时不高亮，不用 G 替代。

点击共享节点优先选择高亮轨迹的截图及终点结果，仍能手动切换。用户显式切换并列轨迹时，若当前节点包含该轨迹，截图同步切换；后台评分刷新不抢走手动选择。刷新保留已展示内容并隔离过期响应，树拓扑未变时保留缩放和拖动位置；仅高亮或分数变化不触发后端重建树、模型请求或新增查询。

本轮仅前端和文档，部署及验证见 [页面交付说明](docs/quality-ui-20261113/README.md)。坐标功能仍依赖上一轮兼容后端；下节的真实网关及点击位置验收不可省略。

### 树图片坐标

默认 `GET /api/data-batches/{batch}/tasks/{task}/tree` 保持树结构并追加坐标能力、tree_version 和 annotation_version。选择步骤后同一路径增加 `view=coordinate`、两个版本、trajectory_id、image，返回单步 coordinate_info 及绑定身份，不新增网关路由。

单步模式复用现有版本索引，不重算整批树或遍历图片。前端以稳定基本值监听，相同内容的新对象不会重发；根节点、关闭 overlay、无坐标动作不查，放大复用数据。切换节点丢弃旧响应；失败保持原因和手动重试，不随状态轮询反复请求。来源未知不猜坐标。

创新平台必须保留自己的 API_BASE_URL 和网关前缀，并按实际 JSON 响应验证本路由；本地测试不能证明 icap 配置正确，HTML/CORS 仍需按真实响应定位。

## 验证与部署资料

本轮定向回归覆盖两引擎真实预处理／建树输入、旧十轨迹 Rubric 复用、原编号及条件冻结、真实隔离子进程与模拟模型、锁竞争、日志异常、取消／重启／迟到结果、无效缓存恢复、坐标版本与前端轮询。日志与测试结果见 [VALIDATION.md](docs/quality-local-20261113/VALIDATION.md)，内网步骤见 [CODEAGENT_PROMPT.txt](docs/quality-local-20261113/CODEAGENT_PROMPT.txt)。

本地测试不调用真实模型、不操作手机；内网用现有十轨迹验证实际 Checklist、F/Q、原件不变及 icap 展示。未完成现场验证前，不宣称内网成功或质检准确率提升。

### 本轮审查修复的原因

| 问题 | 处理 |
| --- | --- |
| 调度台账读取异常后活动槽未释放 | 本地持久租约，领取异常、退出、重启均对账 |
| 日志写入失败导致 PIPE 堵塞 | 持续 drain、及时 flush、明确失败与有界收尾 |
| complete 但无 verdict 的结果永久复用 | 完整契约验证后才缓存，新尝试修复无效结果 |
| 小清单半写后永久冲突 | 原子提交，未完成写入可恢复 |
| Rubric 完成即 95%、部分结果被标成功 | 分阶段真实计数、部分可看、失败明确结束 |
| 所有轨迹技术失败时伪造定义不一致 | 冻结定义为准，优先展示实际 primary_error |
| 原始 case_id 丢失、条件未冻结 | 修复输入身份流转及旧快照恢复 |
| 坐标新对象触发重复请求 | 稳定监听、版本缓存、原 GET 路由单步响应 |

历史说明：早期版本通过 9011 执行 Checklist，曾要求 allowed_roots 和采集端漏参修复。以上远端配置不再是新平台质检的前置条件；仅保留历史任务读取／停止兼容，模型迭代原有流程仍按其专题维护。
