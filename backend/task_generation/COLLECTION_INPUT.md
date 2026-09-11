# 任务生成侧统一采集输入接口

普通任务生成和失败用例扩增使用同一个只读接口，向后续采集模块提供当前保存的未删除任务列表。读取对象是生成侧保存的结果，不依赖先导出 Excel。作业生成成功不代表人工已批准这些任务；接口不推断“已审核”或“可执行”。

## 请求与响应

```http
GET /api/task-generation/jobs/{job_id}/collection-input
```

无请求体，无筛选参数。只接受状态为 `succeeded` 或 `partial` 的作业。成功返回 `200 OK` 和 JSON；成功及错误响应均带 `Cache-Control: no-store`，调用方也不应缓存该地址的响应。

### 顶层字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `schema_version` | integer | 当前固定为 `1`，表示本接口协议版本，与知识库版本不同。 |
| `job_id` | string | 来源作业 ID。 |
| `kind` | string | `task_generation` 为普通任务生成，`augmentation` 为失败用例扩增。 |
| `job_status` | string | 来源作业当前状态，成功响应中为 `succeeded` 或 `partial`。 |
| `knowledge_base_version` | string 或 null | 来源作业保存的知识库快照版本；历史记录未保存时为 `null`。不会替换为当前最新知识库版本。 |
| `task_count` | integer | 本次返回的 `tasks` 长度，包含有效的前置任务行。 |
| `tasks` | array | 全部未删除结果的统一表示，保持结果文件中的顺序。 |
| `errors` | array | 原样返回作业保存的错误列表；字段缺失时为 `[]`。不只保留 `error` 文本，也不移除其中的种子、执行单元等上下文字段。 |
| `warnings` | array | 原样返回作业保存的提示列表；字段缺失时为 `[]`。 |

### `tasks[]` 字段

每条任务均返回以下 15 个字段；未保存的可选值使用 JSON `null`。

| 字段 | 类型 / 取值 | 来源与约定 |
| --- | --- | --- |
| `task_id` | 非空 string | 优先使用结果中非空的 `task_uuid`，否则使用 `result_id`。在本次响应中唯一，不是 GET 时生成的新 ID。 |
| `source_result_id` | 非空 string | 原始 `result_id`，在本次响应中唯一。用于追溯生成侧结果及后续调用现有编辑接口。 |
| `task` | 非空 string | 当前保存的 `task`，包含人工修改。不回退读取 `生成的变体任务` 等中文列。 |
| `app` | 非空 string | 当前保存的 `app`。 |
| `scene` | string 或 null | 当前保存的 `scene`。 |
| `capability` | string 或 null | 当前保存的 `capability`。 |
| `sub_capability` | string 或 null | 当前保存的 `sub_capability`。 |
| `pre_dependency` | string | `zero`、`weak`、`strong`、`pre_node` 或 `unknown`。原字段缺失、为 `null` 或为空时返回 `unknown`。 |
| `pre_task_id` | string 或 null | 原 `pre_task_uuid`，对应同一响应中另一条任务的 `task_id`；无引用时为 `null`。 |
| `source_status` | 任意 JSON 原值或 null | 原 `status`，例如强依赖的字符串 `"-2"`。这是生成结果中的状态，不是采集执行状态。 |
| `source_seed_id` | string 或 null | 原 `seed_id`。扩增任务通常有值，普通生成任务及未保存此字段的历史扩增结果为 `null`。 |
| `source_row` | integer、string 或 null | 原 `source_row`，扩增种子在输入 Excel 中的行号，通常为整数；保留历史字符串值，不从输出顺序重新编号。 |
| `source_task` | string 或 null | 原 `source_task`，通常是源失败任务文本。不从 `task` 或中文列推断。 |
| `case_id` | string 或 null | 原 `用例编号`。它是追溯用编号，不替代 `task_id`。 |
| `dependency_error` | 任意 JSON 原值或 null | 原 `dependency_error`，未保存时为 `null`。 |

“非空”要求字符串不能只包含空白字符；合法字符串返回时保留原有前后空白。空的 `task_uuid` 采用上述 ID 回退规则，空的前置引用返回 `null`。除已说明的字段映射、ID 选择和缺失依赖类型归为 `unknown` 外，接口不额外裁剪任务文本或改写来源字段。`source_node_id`、`execution_unit_id`、完整场景树及种子预览不在这个协议中；不能假设普通生成或历史扩增一定保存了扩增来源字段。

分类字段中的字符串 `Unclassified` 原样保留，不转换为空值或新分类，也不触发重新匹配。`审核状态` 不在统一响应字段中，接口不会把缺失的审核状态补成“已审核”。

## 依赖与有效性

| 依赖类型 | 含义与读取行为 |
| --- | --- |
| `zero` | 已判定无前置依赖，原样纳入任务列表。 |
| `weak` | 必须有 `pre_task_id`，并引用同一响应中有效的 `pre_node` 行。 |
| `pre_node` | 前置任务本身。未删除且数据有效时一并返回，不能只读取主任务而丢弃它。 |
| `strong` | 已判定强依赖，仍返回该任务，并保留原 `source_status`，包括 `"-2"`。接口不替采集端决定是否执行。 |
| `unknown` | 未记录依赖判定，或依赖字段为空。扩增当前不进行普通生成的依赖检查，因此通常为此值；不能理解为 `zero`。 |

删除标记 `deleted` 仅接受布尔值，缺失或 `null` 按未删除处理；其他类型返回 `409`，避免将字符串 `"false"` 错当成已删除。只有 `true` 的行被排除。随后校验整个返回集合：

- 每条任务必须有非空、合法的 `task_id` 和 `source_result_id`；两类 ID 分别在集合中唯一。
- `task` 和 `app` 必须是非空字符串。接口不会跳过坏行后返回一份貌似完整的成功结果。
- 任意非空前置引用都必须能在当前集合中找到目标，且不能引用自己；`weak` 的目标还必须是 `pre_node`。
- 非空且不属于上表的依赖类型视为数据错误，不静默转换为 `unknown`。
- 强依赖、`source_status="-2"` 及 `dependency_error` 不会使一条结构合法的任务自动从集合中消失。
- 如果有效结果文件中的所有行都已删除，返回 `200`、`task_count: 0`、`tasks: []`。这与结果文件缺失或损坏不同。

## 完整响应示例

以下均为示意数据。任务 ID、知识库版本、来源文本和错误信息不是从真实用户数据提取的。

### 普通任务生成

该作业有一条前置任务、引用它的弱依赖主任务，以及一条强依赖任务。普通生成没有失败种子的行号、源任务、用例编号，因此相关字段为 `null`。强依赖仍保留，`task_count` 为 `3`。

```json
{
  "schema_version": 1,
  "job_id": "11111111111111111111111111111111",
  "kind": "task_generation",
  "job_status": "succeeded",
  "knowledge_base_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "task_count": 3,
  "tasks": [
    {
      "task_id": "10000000-0000-4000-8000-000000000001",
      "source_result_id": "10000000-0000-4000-8000-000000000001",
      "task": "在示例笔记 App 中新建一条标题为周末计划的笔记。",
      "app": "示例笔记",
      "scene": "效率工具",
      "capability": "笔记管理",
      "sub_capability": "新建笔记",
      "pre_dependency": "pre_node",
      "pre_task_id": null,
      "source_status": null,
      "source_seed_id": null,
      "source_row": null,
      "source_task": null,
      "case_id": null,
      "dependency_error": null
    },
    {
      "task_id": "10000000-0000-4000-8000-000000000002",
      "source_result_id": "10000000-0000-4000-8000-000000000002",
      "task": "在示例笔记 App 中把周末计划这条笔记改名为周日安排。",
      "app": "示例笔记",
      "scene": "效率工具",
      "capability": "笔记管理",
      "sub_capability": "编辑笔记",
      "pre_dependency": "weak",
      "pre_task_id": "10000000-0000-4000-8000-000000000001",
      "source_status": null,
      "source_seed_id": null,
      "source_row": null,
      "source_task": null,
      "case_id": null,
      "dependency_error": null
    },
    {
      "task_id": "10000000-0000-4000-8000-000000000003",
      "source_result_id": "10000000-0000-4000-8000-000000000003",
      "task": "在示例商城中查询已经发货的指定订单的物流信息。",
      "app": "示例商城",
      "scene": "购物消费",
      "capability": "订单管理",
      "sub_capability": "物流查询",
      "pre_dependency": "strong",
      "pre_task_id": null,
      "source_status": "-2",
      "source_seed_id": null,
      "source_row": null,
      "source_task": null,
      "case_id": null,
      "dependency_error": null
    }
  ],
  "errors": [],
  "warnings": []
}
```

### 失败用例扩增

扩增结果原来没有 `task_uuid`，因此 `task_id` 使用持久化的 `result_id`。该任务未做依赖判定，返回 `unknown`；`pre_task_id` 为 `null`。示例作业部分成功，另一条种子的分类错误和作业提示原样保留，不妨碍读取已经生成的有效任务。

```json
{
  "schema_version": 1,
  "job_id": "22222222222222222222222222222222",
  "kind": "augmentation",
  "job_status": "partial",
  "knowledge_base_version": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "task_count": 1,
  "tasks": [
    {
      "task_id": "20000000000040008000000000000001",
      "source_result_id": "20000000000040008000000000000001",
      "task": "在示例视频 App 中搜索城市漫游并播放第一集。",
      "app": "示例视频",
      "scene": "影音视频",
      "capability": "视频播放",
      "sub_capability": "搜索播放",
      "pre_dependency": "unknown",
      "pre_task_id": null,
      "source_status": null,
      "source_seed_id": "30000000000040008000000000000001",
      "source_row": 2,
      "source_task": "在示例视频 App 中搜索山间故事并播放第一集。",
      "case_id": "EXAMPLE-VIDEO-001",
      "dependency_error": null
    }
  ],
  "errors": [
    {
      "seed_id": "30000000000040008000000000000002",
      "item_id": "8",
      "stage": "classifying",
      "error": "第 8 行种子的分类输出未通过校验"
    }
  ],
  "warnings": [
    "示例作业保存的提示信息"
  ]
}
```

历史扩增没有保存 `seed_id`、`source_row`、`source_task` 或知识库版本时，对应字段直接为 `null`。接口不重新分类，不从用例编号推断它们，也不为了补这些字段写回历史文件。

## 错误响应

| HTTP 状态 | 情况 |
| --- | --- |
| `404 Not Found` | 指定的生成作业不存在。 |
| `409 Conflict` | 作业状态不满足读取条件，包括 `queued`、`running`、`awaiting_confirmation`、`failed`、`interrupted` 等非 `succeeded` / `partial` 状态。即使有检查点结果，也不通过该接口提供采集输入。 |
| `409 Conflict` | 结果文件缺失、无法读取、JSON 损坏、结构不合法，或未删除结果中存在空任务 / App、缺失或重复 ID、非法依赖类型、无效前置引用等数据问题。可选字段或顶层字段类型不符合上述协议时同样返回 `409`，不强制转换类型或悄悄丢弃字段。 |

错误体使用现有 API 的 `detail` 字段，说明具体原因，例如：

```http
HTTP/1.1 409 Conflict
Content-Type: application/json
Cache-Control: no-store
```

```json
{
  "detail": "当前作业状态不允许读取采集输入"
}
```

以上 `detail` 文本仅演示结构；调用方应根据 HTTP 状态处理失败，并展示服务端实际原因，不依赖示例字符串做分支判断。`409` 不会返回删减坏行后的部分 `tasks`。

## 当前视图与后续采集快照

这是生成侧的**动态当前视图**，不是冻结版本，也不会发起采集：

1. 人工通过现有结果编辑接口保存 `task` 后，再次 GET 会读到最新文本，`task_id` 和 `source_result_id` 不因此改变。仅在页面输入但尚未保存的文本不影响此接口。
2. 删除结果后，下次 GET 排除该行；恢复后重新纳入。普通生成现有的依赖组删除 / 恢复行为继续适用，未删除的弱依赖必须仍有有效前置任务。
3. 页面筛选、分页以及上次导出 Excel 的时间不影响本接口。它始终读取当前作业全部未删除结果，且重新计算 `task_count`。
4. 采集模块将来创建执行批次时，应保存本次成功响应作为该批次的执行输入快照，记录来源 `job_id` 和 `schema_version`。执行、重试和审计使用已保存快照，避免生成侧后续编辑改变已创建批次的输入。

GET 本身不调用模型、不进行分类或依赖补判、不生成或补写 ID、不写回结果文件，也不创建导出文件、采集批次或执行快照。强依赖和 `unknown` 如何执行由后续采集流程决定；这个接口负责提供并校验生成侧已保存的数据。

需要保留提交时的任务版本和统一 17 列采集表时，使用独立的 [生成侧采集批次接口](COLLECTION_BATCHES.md)。它通过显式 POST 创建冻结批次，不改变本 GET 的动态只读语义。
