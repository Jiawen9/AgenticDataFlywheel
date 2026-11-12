# 轨迹质检：Checklist 融合与树节点 Q

本次只扩展平台质检和采集端的只读评测服务。Rubric 生成、步骤观察、采集执行、App 复位、模型覆盖和原始轨迹保持原有行为。创新平台前端需按本文适配，不能覆盖其 API 基址、路由或组件体系。

## 分数和推荐

同一任务的候选轨迹共用一份冻结 Checklist。沿用当前模型迭代评测的 gold 优先、否则生成清单的流程；记录真实来源及规则摘要。逐轨迹证据检查及 verdict 仍由现有评测器完成。

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

## 部署与配置

先合入支持 `quality_checklist_jobs.version=1` 的 9011 增量，再同步平台前后端与 CLI。使用“模型迭代评估”已有的评测模型配置；质检页选择评测规则版本，只有一份规则时可自动选择。配置在新质检提交时冻结，续查和重试不读取后来的页面默认值。

平台向采集端传输轨迹身份和原件目录的小清单，不复制图片、不重新采集。采集端必须能只读访问这些原件目录，且目录符合其现有允许读取范围。不同电脑部署时需事先配置共享路径；不以隐式上传或复制作为回退。

## 接口适配

平台新增 `GET/PUT /api/quality-jobs/settings`；保存字段为 `evaluation_profile_id`、`evaluation_profile_digest`。新质检仍通过原提交入口。任务结果新增：

- `scoring_version: checklist_fusion_v1`、`trajectory_order`、`fusion_order`。
- `evaluations[trajectory_id].checklist`：状态、来源、定义摘要、total/passed/failed/unchecked、verdict 和逐项结果。
- `evaluations[trajectory_id].fusion`：version、lambda、valid、score、invalid_reasons。
- `node_scores[node_id]`：q、advantage、scored_count、total_count、status。
- Top-1 项新增 `fusion_score` 和 `ranking_basis`，原 `global_score` 含义不变。

新前端缺少这些字段时按旧 Rubric 数据展示，不用 G 假造 Q 或 F。树的建树分和质检 Q 分别展示。轮询沿用当前页面选择保护；不要因新响应切走用户正在看的轨迹。

9011 独立接口：

| 方法与路径 | 用途 |
| --- | --- |
| POST /quality-checklist-jobs | multipart：request JSON 文本 + manifest_file UTF-8 JSON 文件；异步受理 |
| GET /quality-checklist-jobs/{id} | 小型状态摘要 |
| GET /quality-checklist-jobs/{id}/results?offset=0&limit=50 | 分页读取结果 |
| GET /quality-checklist-jobs/{id}/reports/{report_id} | 只读评测报告 |
| POST /quality-checklist-jobs/{id}/cancel | 仅停止对应质检评测 |
| POST /quality-checklist-jobs/{id}/retry | 重试未完成部分，复用已完成结果 |

请求包含 request_id、evaluator_config、evaluation_profile_id/digest；清单包含 schema_version、quality_job_id、input_digest，以及各 task_id/case_id/task/key_elements 和 trajectory_id/source_directory。相同请求编号与内容重放复用任务，改变内容冲突。运行数据和规则快照属于采集端独立质检任务，不伪装成手机采集运行。

## 验证范围

定向测试覆盖公式、全通过优先、原表同分顺序、无效分数、真实建树 occurrences、Q 去重、分支不均衡、A 边界、历史选择保护和 CLI multipart。另验证采集端排队、重放、取消、重试、只读原件，以及前端类型检查和构建。具体执行结果见交付验证记录。内网真实模型和网关验收需单独完成，离线测试不代表质检准确率或设备验收。
