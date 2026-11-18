# 轨迹树纠偏与 OPD 导出

## 轨迹树纠偏

- 每次按需读取一个任务。横向展示原始动作先后、路径和分支 Q，菱形标在有可比较 Q 差异的实际动作上。展开任务时优先定位所选轨迹的第一个关键动作，侧面板保持收起。
- 点击共享节点时优先定位当前轨迹。面板列出经过该节点的轨迹及对应实际步号，一次提交只保存当前轨迹的该步。上一步和下一步按此轨迹的真实步骤顺序移动。保存后树上的小勾显示生效的修改。
- 质检页进入纠偏携带 `batch_id`、`correction_task_id`，可附 `correction_trajectory_id`、`correction_step`。自动 Pipeline 继续传现有的 `pipeline_id` 和阶段上下文。
- 树数据来自 `GET /api/correction/sessions/{session_id}/tasks?view=tree&task_id=...`，可附 `expected_revision`。响应提供原树、树与评分版本、节点分、`key_node_ids` 和按节点组织的 `targets`。每个目标包含会话内的 `group_id`、`trajectory_id`、`step`、`step_key`、`excel_row` 以及修订状态。映射不足或步骤未入树时，可通过现有步骤表修正。

## 导出

- 新 OPD 通过现有导出或 Pipeline 创建接口提交 `export_profile: "opd_prefix_v1"` 和 `cot_policy: "skip"`。每条入选轨迹从起点保留到首个有效动作修正步，包含该步的修正动作。只改 SOP、动作恢复原值或没有动作修改时保留所有未删除步骤。轨迹摘要中的 `opd_cutoff_step` 给出本次草稿的截止步，空值表示保留全部未删除步骤。
- Excel 和配套 JSONL 使用同一份冻结步骤，thought 留空。证据图片沿用完整原始轨迹目录。旧客户端提交 `corrected_selection_v1` 时仍得到原来的完整选轨语义，历史 Pipeline 和冻结发布仍按原策略读取。
- 创新平台接入时沿用其 API 基础地址与原路由。树选择以会话返回的轨迹步骤对应关系为准；进入、修正和勾选导出使用各自的现有操作。采集服务继续使用现有协议。

## 验证

本地后端 169 项、前端 134 项定向用例通过，Vue 类型检查及生产构建通过。使用共享分叉样例在 Chrome 检查桌面和窄屏交互，并验证实际请求只更新选定轨迹。使用本地 Excel 与 JSONL 验证同一截断范围、历史策略兼容和完整图片目录读取。真实内网批次与云道上传待现场验收。
