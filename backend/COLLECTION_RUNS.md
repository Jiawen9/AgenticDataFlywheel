# 采集运行与轨迹完成登记协议

一个已提交的生成／扩增批次可以执行多次采集，每次正式运行分配独立的 `collection_run_id`。批次任务来自冻结的 `batch.json`，`collection_case_id` 对应采集 Excel 的“用例编号”；后端用它恢复原 `task_id` 和 `source_result_id`，不依赖轨迹目录名字猜测任务。

## 下发

沿用 `POST /api/phone-factory/remote/start-run`。批次文件必须先经过现有 `/tasks` 上传登记，且字节 SHA256 与生成侧冻结的 Excel 相同。请求示例：

```json
{
  "filename": "collection-batch-batch-one.xlsx",
  "phone_id": "phone-1",
  "app": "Demo",
  "request_id": "13f64044-2bc1-46b1-b9d0-5d4a2ba97f43"
}
```

`request_id` 是调用方为本次操作生成的唯一键；同一次请求的重试保持不变，新一次正式运行换新键。允许字母、数字、`-`、`_`，最长 128 字符。服务重启后相同键仍返回同一运行。相同键对应不同手机、App、关联关系、采样配置或输入文件时返回 `409`。旧调用省略该字段时每次请求产生新运行。也接受同义的 `dispatch_key`。

批次任务的成功响应保留手机服务原回执，并增加 `batch_id`、`collection_run_id`、`output_dir`、`collection_status`。重复请求已下发的运行只返回原回执；正在下发时返回相同编号和 `dispatch_pending=true`，不重复启动。下发失败后用相同键重试会复用原运行。手机服务也必须按 `collection_run_id` 去重，避免网络超时但已接收时重复执行。

后端 Python client 向手机服务原 `/start_run` multipart 增加三个文本字段：

| 字段 | 含义 |
| --- | --- |
| `batch_id` | 已提交的采集批次编号 |
| `collection_run_id` | 本次采集运行编号，重试不变 |
| `output_dir` | 本次原始轨迹输出根目录的绝对路径 |

现有 `task_file`、`apps_file`、手机、App、采样和经验库参数保持原样。手动上传任务继续使用原协议，不增加这三个字段。手机服务接入方需要落实输出目录和完成回传；仅返回下发成功不会使轨迹进入预处理。

## 原始文件归档

默认布局：

```text
backend_workspace/raw/collection_batches/<batch_id>/runs/<collection_run_id>/
  <collection_case_id>/<原轨迹目录>/
    _trajectory_for_evaluate.json
    step001_vla_model_response.json
    step001_vla_input.jpg
    step001_vla_input_stability.jpg
    step001_vla_input_ui.xml
    ...
```

`ADF_DATA_ROOT` 可指向项目外目录；实际以收到的 `output_dir` 为准。远端手机服务必须先把文件放到本后端可访问的该目录，再登记完成；路径字段本身不会上传或同步文件。同用例的多条轨迹使用不同原轨迹目录。同名原轨迹可出现在不同运行中，不会合并覆盖。

只回传最终轨迹目录，运行中目录不得登记。用例编号和目录必须是安全的单个文件名；不得含 `/`、`\`、`..`、Windows 保留名等，大小写不同但实际文件系统冲突的用例编号会被拒绝。不要为不合法编号自行换号，应修正生成侧数据后创建新批次。

## 完成回传

调用 `POST /api/phone-factory/collection-runs/{collection_run_id}/complete`：

```json
{
  "batch_id": "batch-one",
  "collection_run_id": "cr_由后端返回",
  "trajectories": [
    {
      "collection_case_id": "CASE-01",
      "source_trajectory_id": "original-run",
      "relative_dir": "CASE-01/original-run",
      "collected_at": "2026-09-15T13:00:00+08:00",
      "files": [
        {
          "path": "CASE-01/original-run/_trajectory_for_evaluate.json",
          "sha256": "文件真实字节的64位十六进制SHA256",
          "size": 1234
        }
      ]
    }
  ],
  "errors": [
    {"collection_case_id": "CASE-02", "error": "手机断开连接，未产生最终轨迹"}
  ]
}
```

示例省略了其余文件；真实 `files` 必须列出该轨迹目录内的全部文件，且与磁盘内容完全一致。所有 `path` 均相对于本次 `output_dir`，使用 `/`，包含 `用例编号/原轨迹目录/` 前缀。`relative_dir` 必须恰为 `用例编号/原轨迹目录`。禁止绝对路径、跨用例路径、重复项、符号链接和目录联接。

`source_trajectory_id` 也可用 `trajectory_id` 传入；缺省采用原目录名称。`collected_at` 可省略，后端使用首次完成时间；显式时间必须带时区。`size` 可省略，SHA256 必填。`errors` 可省略或为空；其中用例编号可以省略，表示整个运行的问题。任务失败不能通过冒填一份轨迹表示。

后端核验已登记批次与运行、用例归属、完整文件清单、SHA256，以及 evaluation JSON 和可转换的步骤文件。通过后将运行标为 `completed` 并冻结清单。响应中的 `trajectories[]` 增加生成侧 `task_id`、`source_result_id`、任务文本和 App；不会采用回传方提供的生成侧身份。

首次和相同清单重复回传都返回 `200`；重复回传不更新时间、编号或清单。已完成清单不允许增删、替换轨迹或更改错误。后续补采必须创建新的采集运行。部分成功允许登记；`trajectories=[]` 也可以完成，但没有可用轨迹的批次不能启动预处理。

| 状态码 | 含义 |
| --- | --- |
| `404` | 批次或采集运行不存在 |
| `409` | 批次、运行、用例归属不符；文件缺失或清单／SHA不符；路径不安全；已完成清单冲突；无可用于预处理的轨迹 |
| `502` | 手机服务下发失败，运行保留错误，可按同请求键重试 |

## 查询与预处理读取

- `GET /api/phone-factory/collection-runs?batch_id=<id>` 返回 `{"runs": [...]}`，省略批次则列出全部新运行。
- `GET /api/phone-factory/collection-runs/{run_id}` 返回当前登记及完成清单；不存在为 `404`。
- 运行状态为 `dispatching`、`running`、`failed` 或 `completed`。只有 `completed` 清单被预处理读取；下发成功和手机页面“运行中”不代表采集完成。

Python 调用 `CollectionRunStore(root).ready_input(batch_id)` 或 `freeze_ready_input(batch_id)` 得到只读值：

```text
{
  schema_version: 1,
  batch_id,
  raw_root: <data>/raw/collection_batches/<batch>,
  input_digest,
  runs: [{collection_run_id, completed_at, manifest_sha256, trajectory_count}],
  trajectories: [{collection_run_id, collection_case_id, task_id,
                 source_result_id, source_trajectory_id, relative_dir,
                 collected_at, files: [{path, sha256, size?}], task, app}],
  errors: [{collection_run_id, collection_case_id, error}]
}
```

该调用重新验证文件 SHA，不创建作业、不调用模型、不写文件。预处理任务负责将返回值冻结到自己的记录；后续新采集运行不会改变已经冻结的输入。`raw_root` 是批次根，而文件路径相对具体运行，因此访问路径为 `raw_root/runs/<collection_run_id>/<file.path>`。输入摘要不含数据根目录位置；相同内容迁到另一配置根目录仍可稳定核验。

建树保存本次批次的 `raw_root`、标框版本及冻结来源 JSON。质检进入修正时使用 JSON 中显式的任务和稳定轨迹编号，跨运行同名目录保持独立；修正会话进一步冻结资源根目录，截图接口和 COT 都从该目录取图。身份信息保留在 JSON 中，不加入原有业务 Excel 表头。已有轨迹批次原本没有这些身份字段时保留其原编号，不对旧文件补写或重编号。

当前状态及已完成清单保存在 `<data>/system/app.sqlite` 的 `collection_runs` 命名空间，不另存可变状态 JSON。批次来自 `<data>/system/task_generation/collection_batches/<batch>/batch.json` 的冻结快照；旧目录不会参与登记和读取。已采集后的读取不依赖采集 Excel 存在，但首次下发仍需要准确的冻结 Excel。源文件在完成后发生变化会阻止预处理，不自动用新内容覆盖原清单。

## 无网络自测

```powershell
python -m unittest backend.tests.test_collection_runs backend.tests.test_phone_factory -v
python -m backend.tests.collection_run_demo
```

模拟示例只在临时目录创建两条任务和一个小型原始轨迹；手机 client 被固定的本地函数替代，完成回传通过本地服务方法执行，不连接真实手机、模型或外部网站。示例退出后删除自己的临时数据。
