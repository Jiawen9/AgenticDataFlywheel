# 独立手机工厂服务（9011）

此服务接入已有 `new-guigent` 执行模板，提供采集、ADB 监控、模型迭代评估和可校验的 HTTP 结果回传。平台仍使用自身 FastAPI、SQLite 和批次体系。采集服务器无需挂载平台磁盘，也不依赖 S3。

## 部署

在采集服务器部署本仓库的 `backend` 代码。创建独立 Python 环境后，安装本目录的 `requirements.txt`，并安装执行模板原有依赖。模板中的 `main.py`、`config_trajectory.py`、`config_report.py`、`run_evaluator_batch.py` 和 `yadb` 来自采集服务器已有工程，本次不随仓库分发。

必须配置：

| 环境变量 | 含义／默认值 |
| --- | --- |
| `PHONE_FACTORY_ROOT` | 服务独立持久目录，默认当前目录下 `phone_factory_workspace` |
| `PHONE_FACTORY_TEMPLATE_DIR` | 服务器上的 `new-guigent` 模板绝对路径，必须配置 |
| `PHONE_FACTORY_ADB` | ADB 命令或绝对路径，默认 `adb` |
| `PHONE_FACTORY_EXECUTION_TIMEOUT` | 单设备采集最长秒数，默认 86400 |
| `PHONE_FACTORY_EVALUATOR_TIMEOUT` | 单设备评估最长秒数，默认 3600 |
| `PHONE_FACTORY_ADB_TIMEOUT` | 监控 ADB 命令超时，默认 10 |

模型端点、密钥和模型名称沿用服务器的模板／环境配置。仓库没有写入同事机器的 IP、API key 或 Android SDK 路径。VLA 端点由平台本次运行选择传入；其它模型配置不会由服务猜测。

从仓库根目录启动：

```sh
python -m uvicorn backend.phonefactory_manager:app --host 127.0.0.1 --port 9011 --workers 1
```

对外部署通过已有网络入口转发到 9011。不要开启自动 reload；执行任务可持续数小时。采集目录与平台 `backend_workspace` 必须分离。停止服务不会把正在运行的任务当作成功；重启后会根据持久进程身份和完成记录继续归档。未能确认启动的运行明确失败且不会再次下发手机，检查日志后使用新运行编号重试。

## 执行和目录约定

平台上传原始任务 Excel（现有 17 列均可保留）及手机/App 关联 JSON。必需列为 `用例编号`、`涉及APP`、`任务`。业务批次协议还必须携带逐任务冻结清单，校验表格用例、任务文本和 App 完全一致。

每台手机有独立模板副本。ADB serial 保留原值，包括无线 ADB 的 `IP:port`；本地设备目录使用稳定摘要，避免串目录。运行参数使用环境变量传入，不拼接可执行 Python 字符串。

单设备运行依次完成：过滤本设备用例写 `test.xlsx` → ADB/yadb 预检 → 复制采集配置 → `main.main()` → 切换评估配置 → `run_evaluator_batch.py` → 恢复采集配置 → 写入完成记录。失败时保留原始输出和日志。新的运行将旧 `.runs` 和旧报告移入该运行的诊断目录，不把旧结果作为新结果。

模板须输出以下任一结构，目录用例必须与任务清单准确对应：

```text
.runs/<timestamp>/<collection_case_id>/<source_trajectory_id>/
.runs/<collection_case_id>/<source_trajectory_id>/
```

每条轨迹包含 `_trajectory_for_evaluate.json`、最终步骤模型响应、对应输入图像和 XML。服务复用平台现有步骤解析验证规则；PID 消失、退出码为 0、设备空闲均不等于采集成功。无法关联任务、缺失文件或报告执行错误会进入错误清单，有可用轨迹时状态为 `partial`。

```text
PHONE_FACTORY_ROOT/
  collector.sqlite
  devices/<device_digest>/          # 独立执行模板和当前日志
  runs/<collection_run_id>/
    tasks.xlsx, apps.json, request.json
    execution/<device_digest>/     # 进程身份、完成记录、未删原件
    frozen/
      manifest.json
      archive.zip
      raw/<case>/<device_digest>__<source_trajectory_id>/...
      raw/reports/<report_id>/<report_filename>
```

删除设备会先停止包含该设备的活动运行（多手机运行整体中断），确认所属进程停止后移除该设备副本；独立 `runs/` 归档保留。停止失败会返回错误，不回报删除成功。

## HTTP 协议

- `GET /capabilities`：`protocol_version: 1`、`batch_results: true`。
- `POST /start_run`：multipart `task_file`、`apps_file`、`vla`、`runmode`（`generate` / `modeliter`）、`sampling_enabled`、`temperature`、`top_p`、`use_experience_lib`、可选 `phone_id` / `app` 筛选，以及 `collection_run_id`、`batch_id`、JSON 字符串 `task_manifest`。
- `task_manifest` 形状为 `{tasks: [{collection_case_id, task_id, task, app, ...来源分类字段}]}`。生成模式必须传批次；评估模式可不传批次。所有实际运行参数、文件摘要和任务清单参与幂等校验。
- 相同运行编号和相同请求返回已有运行；参数冲突返回 409，不重复执行。服务先持久登记，随后异步执行。
- `GET /runs/{id}`：返回 `run_id`、`collection_run_id`、`batch_id`、`run_mode`、`status`、`errors` 和可用时的 `manifest`。
- 状态为 `queued / running / succeeded / partial / failed / interrupted`。远端执行完成后，平台还需下载、校验、登记完成，才能在预处理里使用。
- `GET /runs/{id}/archive`：下载冻结 ZIP。manifest 中逐文件记录相对路径、size、sha256；顶层 archive 记录 ZIP 自身 size 和 sha256。ZIP 仅包含清单中的轨迹和报告文件，不含 manifest，避免摘要循环。
- 平台旧 `output_dir` / `result_root` 字段仅兼容接收，不会用于远端写入路径。无业务编号的旧调用分配独立 `legacy_...` 运行；不自动登记成平台批次。

兼容设备与监控：

- `POST /add_phone` / `del_phone`：JSON `{phoneid}`。
- `POST /status`：JSON `{phones: [...]}`，返回 `statuses: [{phone_id,status}]`。
- `POST /adb_devices`：返回 `devices: [{serial,model,battery}]`。
- `POST /monitor`：JSON `{phone_id}`，返回 base64 PNG 或 null、日志、running、device_size。

评估报告：

- `GET /reports?runmode=modeliter`：返回 `folders`，每个文件附稳定 `run_id` / `file_id`。
- `GET /report_download?runmode=modeliter&run_id=...&file_id=...`：校验冻结摘要后下载。
- 兼容旧 `folder` / `name` 查询；同名报告不唯一时要求使用稳定 ID。生成与评估查询互不混入。评估不自动进入平台生产批次。

## 本地验收

```sh
python -m unittest backend.tests.test_collector_service -v
```

测试使用注入的设备／执行适配器和临时 SQLite，包括真实 HTTP multipart、双手机同名轨迹、归档 SHA、幂等冲突、并发下发、空结果、部分失败、报告隔离、删除失败、队列重启恢复和文件冻结后数据库提交中断。不调用真实 ADB、模型或上传服务。

真机联调先核对服务器模板输出路径及任务清单，再验证多手机回传完整性。若模板使用不同目录布局，应增加明确的用例映射输出，不能根据任务文字、序号或最新目录猜测身份。
