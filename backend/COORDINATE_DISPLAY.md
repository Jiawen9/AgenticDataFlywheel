# 截图坐标展示及人工修正适配（2026-11-09）

本次修改平台后端与自用前端。创新平台前端按下述契约适配，保留它原有的 API_BASE_URL、`/v1/` 网关前缀、路由和组件体系。平台 CLI、历史轨迹、标框与评分不批量改写。

## 按可见步骤读取坐标信息

普通轨迹、质检树、中间态审计、COT 和放大预览使用同一个查询：

```http
POST /api/coordinate-info
Content-Type: application/json

{"batch_id":"b...","annotation_version":"已加载的标注版本","trajectory_id":"已加载的轨迹编号","image":"该步骤已登记的相对图片路径"}
```

`image` 是已登记步骤的身份，不接受任意本机文件路径；无需传动作或 XML 让前端猜测。返回示例：

```json
{
  "annotation_version": "已加载的标注版本",
  "coordinate_info": {
    "space": "pixel",
    "width": 1440,
    "height": 3200,
    "reason": "pipeline_executed_input_image"
  }
}
```

`space` 为 `pixel`、`normalized` 或 `unknown`。来源未确认时 `unknown` 携带 `reason`，不按数值是否超过 999 来猜。Pipeline 通过登记来源、执行响应中的 `actual_action_coordinate_system=pixel`、步骤编号、原始 `executed_action` 核对来源，再读取原样保存的执行输入图尺寸。人工修改后的动作使用服务端保存的坐标契约核对。XML 节点范围不作为屏幕尺寸，缺 XML 不阻止这类已确认输入图显示。

修正会话的当前步骤使用：

```http
GET /api/correction/sessions/{session_id}/rows/{excel_row}/coordinate-info?expected_revision={storage_revision}
```

返回 `coordinate_info` 和 `storage_revision`；会话版本不符返回 409。前端刷新当前会话后重新查询，不用旧响应覆盖新选择。普通批次查询版本无效同样返回 409；步骤不存在返回 404。

缓存键必须包含批次、标注版本、轨迹和图片；修正会话包含会话、行号和修订号。只对当前显示的步骤查询，折叠项/关闭抽屉不批量预取；当前步骤变化后丢弃迟到响应。请求“最新版本”但未携带版本时不持久缓存。后端使用有界版本索引，同一冷索引的并发查询合并构建；不会每个步骤都重新遍历整批或读取全部截图/XML。

## 显示与编辑

像素坐标映射到实际图片内容区域：`显示x=x/width×显示图宽`，`显示y=y/height×显示图高`。计算鼠标点时用图片本身的矩形，不用带留白的外层容器；反向使用源 width/height，不能统一乘 999 或截断到 999。点击、长按、滑动的起终点以及放大预览必须共用此逻辑。标框继续按既有图片像素坐标显示。

明确 NewAgent/已验证 native 格式沿用旧约定：展示除以 1000，人工取点范围 0–999；本次不重构其历史行为。没有来源字段本身不能证明是 NewAgent，先等待后端来源判定。

人工修正仍调用现有行 PATCH 接口提交动作与 `expected_revision`。服务端核对动作是否真正改变了坐标，并自动保存坐标系、源画布、动作摘要和来源；客户端不能自报像素坐标系绕过判定。保持不相关动作字段；未修改动作时保留原串和原值。SOP、文本等非坐标修改不要求坐标来源完整。已确认来源在切换为空间/非空间动作时继续保留；旧修正无法确定时，仅限制会改变坐标的操作，保留截图、标框和原值。

纠偏导出增加 `coordinate_info` 扩展列（有新契约时），完整表和训练分流表均保留，重新打开后仍按同一源画布显示。不得主动补写旧修正或重算历史标框/评分。

## 部署与验证

先部署兼容后端，再同步对应前端。新版前端遇到未知来源或查询失败时不把 Pipeline 默认为归一化。实际内网用一条已有 Pipeline 轨迹核对点击/滑动显示、放大预览和人工取点保存重开即可，不要求重新采集整批。

另外观察到标框复核代码有独立坐标风险：`backend/bounding_box/action_box.py` 的 `screen_size` 使用 XML 节点最大边界；`qwen_reviewer.py` 有直接在图片绘制动作点的路径。XML 内容覆盖范围可能小于截图，需另行核对源画布语义。本次不据此声称历史评分全部正确或错误，也不自动重算评分。
