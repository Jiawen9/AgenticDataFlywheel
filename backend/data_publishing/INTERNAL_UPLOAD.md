# 云道S3 Excel 上传接入

云道S3是目标网站名称。接口中的 `target: "internal"`、`internal_upload` 字段及适配模块名称保持不变；
此命名不表示已配置网站连接，也不会将现有上传协议改为通用 S3 API。

页面、后台作业、文件定位、校验、逐文件回执、重试和刷新恢复已接通。接入方只需实现本目录的
`internal_uploader.py`，与现有 Python 后端一起部署运行。默认实现始终未配置，不发送网络请求。
本接口只上传发布登记的全部 Excel；不会上传原始轨迹目录、合并表格或转成 JSON。

## 同事需要实现什么

1. 在 `is_configured() -> bool` 中检查后端所需配置是否就绪。返回真正的布尔值；不要在此上传文件。
2. 在 `upload_excel(context: UploadContext) -> UploadResult` 中完成登录和单份 Excel 上传。
3. 凭据从后端环境变量或公司凭据服务读取，配置名称由目标网站接入方确定。不要传给浏览器或写入回执。
4. 为登录、连接和读取设置有限超时。只有目标网站明确确认接收成功后，才能返回 `UploadResult(success=True)`。
   HTTP 200 或“提交到异步队列”本身不一定代表文件已被接收，应按目标接口的真实语义确认。
5. 失败抛出 `UploadError("可直接展示给用户的原因")`。不要包含密码、令牌或原始 HTTP 响应。
   未预期异常只显示通用错误。回执 ID 必须是字符串；链接必须是无用户名密码的 HTTP(S) 地址。

`DATASET_UPLOAD_MODE=mock` 仅控制历史模拟接口，**不会启用云道S3上传**。
本次没有新增虚假的“启用开关”；接入完成并配置凭据后，由 `is_configured()` 返回 True。
重启后端并刷新页面即可更新按钮可用状态。

## 系统已解析的调用参数

| context 字段 | 类型及含义 |
| --- | --- |
| file_path | Path，已解析并核验的绝对 Excel 路径；可直接以 rb 打开 |
| filename | str，登记的原始文件名 |
| dataset_name | str，数据集名称 |
| release_id | str，发布编号，如 rel_0123456789abcdef |
| file_index | int，登记清单中的位置，从 0 开始；不同文件可能同名 |
| sha256 | str，发布时的 SHA256，上传前已核验 |
| idempotency_key | str，`发布编号:文件序号:小写SHA256`，重试保持不变 |

无需读取 releases.json、解析发布编号、查找目录或重新导出 Excel。
函数返回示意：

```python
return UploadResult(
    success=True,
    remote_id="目标网站返回的记录编号",  # 可省略
    url="https://目标网站/记录地址",     # 可省略
)
```

应把 `context.idempotency_key` 传入目标网站的幂等机制，或由接入方按该键查重。
远端已接收但本地尚未保存回执时，网络超时或进程退出会造成结果不确定；重试仍用同一个键。
仅靠本地成功记录无法保证远端绝不重复。适配器不得修改源 Excel。

## HTTP 协议

- `GET /api/dataset-upload-capabilities`：
  `{"internal":{"configured":false,"reason":"云道S3上传尚未配置"}}`；已配置时 reason 为 null。
- `POST /api/dataset-releases/{release_id}/upload`，JSON `{"target":"internal"}`：
  202 返回 `{"job":{...}}`。同一发布已有排队中、上传中或已成功任务时返回该任务。
  缺少发布记录 404；未配置、无 Excel、缺失文件或 SHA256 校验失败 409；不合法 target 422。
  不传 body 或 target 的旧调用仍走模拟上传，显式 `target:"mock"` 也支持。
- `GET /api/dataset-upload-jobs/{job_id}`：返回 `{"job":{...}}`；不存在 404。
- 发布列表及详情增加可选 `internal_upload` 摘要。历史无该字段表示尚未上传云道S3。
  原 `upload_status`、`s3_uri` 等保留为模拟状态，不能作为真实接收凭据。

旧版本保存的系统提示由页面按精确匹配显示为云道S3文案，不改写原记录。
适配器的其他错误原文、文件名、远端编号及链接保持原样。

内部任务示例（部分成功后停止）：

```json
{
  "job_id": "0123456789abcdef0123456789abcdef",
  "release_id": "rel_0123456789abcdef",
  "mode": "internal",
  "status": "failed",
  "current_file": "第二份.xlsx",
  "completed_files": 1,
  "total_files": 2,
  "percent": 50,
  "error": "目标网站暂不可用",
  "file_results": [
    {"index": 0, "filename": "第一份.xlsx", "sha256": "<64位校验值>", "idempotency_key": "<稳定键>", "status": "succeeded", "remote_id": "record-123", "url": "https://internal.example.test/records/123", "error": null},
    {"index": 1, "filename": "第二份.xlsx", "sha256": "<64位校验值>", "idempotency_key": "<稳定键>", "status": "failed", "remote_id": null, "url": null, "error": "目标网站暂不可用"}
  ]
}
```

任务及文件状态为 queued / uploading / succeeded / failed / interrupted；文件未处理时为 pending。
百分比按已确认成功的文件数计算，不表示网络传输百分比。
多份表格按登记顺序串行执行，一份失败后停止。重试创建新任务、保留旧任务记录，
跳过已确认成功且校验值一致的文件；失败文件重用原幂等键。全部成功后不会再次启动。

## 持久化与恢复

- 发布登记及摘要：`backend_workspace/dataset_release/releases.json`
- 完整任务及逐文件回执：`backend_workspace/dataset_release/upload_jobs/{job_id}.json`
- Excel 使用登记中的原导出路径，不复制、不覆盖已发布文件。

提交检查和任务登记使用现有作业管理器锁；该模式适用于现有单后端进程。
每份成功回执先原子落盘，再更新发布摘要。重启后依据最新任务修复摘要，
执行中的任务标为中断，保留已成功回执，等待用户手动重试。上传不会自动重启。
如果未来部署多个后端进程，需要共享任务队列和跨进程锁，不能仅复用进程内锁。

## 无网络自测

可运行：

```powershell
python -m unittest backend.tests.test_internal_dataset_upload backend.tests.test_data_publishing -v
cd frontend
npm test
npm run build
# 使用本机已安装的 Playwright（必要时配置 NODE_PATH）
node tests/dataset-upload.browser.cjs
```

`backend/tests/test_internal_dataset_upload.py` 包含可直接参考的 `FakeAdapter`：
实现相同的两个入口，通过构造参数 `DatasetUploadJobManager(..., internal_adapter=adapter)`
注入隔离测试管理器。它仅在临时数据下返回明确标注的测试回执，不会自动进入生产路径。
不要把这个测试适配器用于真实发布数据。

完成真实接入后，先在目标网站测试环境验证登录、单表接收、多表、超时、同键重试查重及可展示错误，
再配置正式后端。本次自动测试不调用真实网站。
