# 云道发布上传接入

平台调用内网已有的 `s3_uploader.py`，直接读取冻结发布 Excel 与该表包含的完整轨迹目录。不会复制整批数据、打 ZIP、修改 Excel、生成云端清单或重新进行 XML／动作／图片质量检查。未配置时不会启动脚本。

## 后端配置

安装平台 `backend/requirements.txt`（本次新增 `psutil`，用于进程身份核对）。在实际运行平台后端的电脑配置环境变量或 `backend/.env`，环境变量优先，显式空值会禁用同名 `.env` 值：

```dotenv
ADF_YUNDAO_PYTHON=D:\python-env\python.exe
ADF_YUNDAO_SCRIPT=D:\upload-tools\s3_uploader.py
ADF_YUNDAO_APP_TOKEN=<由内网维护人员填写，不提交到 Git>
ADF_YUNDAO_REGION=cn-southwest-204-dev
ADF_YUNDAO_BUCKET=bucket-jinglun
ADF_YUNDAO_PREFIX=data/gui_data/e2e_data/flywheel_data
```

Python 必须能运行该脚本及其依赖。后端运行账号必须能读取源目录；桌面登录用户的映射盘不一定对服务账号可见。目标前缀可在上传弹窗修改，空白表示 bucket 根目录；bucket、region 由后端配置。

启动参数沿用：`--local_folder_absolute_path`、`--app_token`、`--region`、`--bucket_name`、`--bucket_path`、`--show_speed`。参数数组启动、不开 shell。进程先等待平台持久登记身份，然后执行原脚本。token 不返回前端、不存入作业记录、不写入日志；脚本输出在截断前脱敏。由于私有脚本使用命令行参数接收 token，操作系统中有权限读取进程参数的本机管理员仍能查看它。

本地不包含该私有脚本。内网接入只需确认两项契约：

1. `bucket_path` 是**目录内容**的目标位置，不再自动多加本地目录 basename。若原脚本自动加 basename，在薄适配层修正，不复制源目录来迎合参数。
2. 退出码 0 表示所有文件已经完成上传；失败要返回非零。如果只提交远端队列或吞掉单文件失败，接入层应等待最终结果／汇总失败后返回，不能直接宣布成功。

## 上传范围与布局

根据冻结 Excel 实际行及其发布来源解析路径；不读当前页面勾选，不使用可能指向整个共享根目录的旧 `trajectory_paths`。一用例 10 条、表内选 2 条，只上传选中的 2 个完整目录，目录内所有现存文件和子目录交由原脚本传输。旧“完整数据集”表本身可能含全部轨迹，页面展示实际解析出的范围。

```text
<前缀>/<批次编号>/
  发布表格/001/<原文件名.xlsx>
  发布表格/002/<另一原文件名.xlsx>
  <用例编号>/<原轨迹目录名>__<稳定来源摘要>/...
```

摘要后缀用于区分同名目录、不同手机、重复采集和运行，重试保持相同目标。多批次发布分别进入对应批次目录；重复引用同一轨迹只上传一次。完整目录的传输不受坏 XML、损坏图片或未在表中引用的文件影响。轨迹身份无法定位、越界或本地目录不存在会明确失败，不能改为上传整个共享目录。

Excel 已冻结在 `<DATA_ROOT>/releases/<release>/<序号>/<原文件名>`；轨迹仍在既有原始目录。后台逐目录串行调用，不额外创建合并目录。Excel 保持原字节和内部路径字符串，云端上传不会自动重写表中本地绝对路径。

## HTTP 与前端适配

沿用原 API 地址及网关前缀，不新增网关路由：

- `GET /api/dataset-upload-capabilities`：`internal` 增加 `directory_upload`、`default_prefix`、`region`、`bucket`，保留 `configured/reason`；不返回 token、Python 或脚本路径。
- `POST /api/dataset-releases/{release_id}/upload`：`{"target":"internal","include_trajectories":true,"destination_prefix":"data/gui_data"}`。持久登记后立即返回 202 和 job，不等待 Excel 解析或文件传输。
- `GET /api/dataset-upload-jobs/{job_id}`：复用轮询，任务包含 `folder_upload`、`include_trajectories`、`destination`、`source_summary`、`item_results`、`completed_items/total_items`、`current_item`、`progress`、`recent_output`。
- 同目标失败／中断重试可附 `retry_job_id`。后端核对发布、范围和原目标；bucket／region 已改变时明确报错，不能静默上传到新 bucket。更改前缀创建新任务，不携带旧重试编号。
- 原 `target:internal` 的 Excel-only 调用继续支持；配置本次脚本后同样走可登记进程的后台目录任务，仅传表格，保留旧计数／回执投影。未带 target 的历史模拟接口保持原行为，模拟结果不代表真实云道上传。

新目录任务阶段：`queued → resolving → uploading → succeeded/failed`。重启若旧进程仍存在或身份读取暂不可用，显示 `recovering` 并继续查询，不能启动第二份；确认进程退出但没有完成回执时标为 `interrupted`，由用户重试未确认目录。

`percent` 是完成目录比例；`progress` 中的速度和字节数据只来自脚本实际输出。纯百分比文字不作为字节总进度，只有可信已传／总字节时才展示字节百分比。兼容 CR、LF、CRLF 和 UTF-8 分段输出；未知格式仍展示脱敏文字。当前目录的字节进度不冒充整批总进度。

页面关闭不终止上传；刷新恢复原任务。目录回执默认折叠、每页 10 条，最近输出最多 20 行。进度小记录最多每秒保存一次（目录结束另保存最终值），不随每行速度输出重写全部目录清单。

## 幂等、恢复与部署

同一冻结发布、范围、region、bucket 和前缀复用任务；新目标独立上传。失败重试沿用已解析来源和目标，跳过已确认成功目录，不重新选轨迹。远端已收、平台还未保存确认时，可能重传该未确认目录；目标键保持稳定，需内网脚本确认同键覆盖／幂等语义，平台不承诺远端恰好一次。

作业、回执和轻量进度使用平台既有 SQLite；不新建运行数据库、不修改采集记录。当前调度适用于现有单后端进程；多服务进程的分布式执行协调不在本轮范围。

先同步后端代码、安装依赖并配置私有脚本，核对能力；再同步前端。创新平台只合并业务组件、类型和调用字段，保留自身 API_BASE_URL、路由及 UI 体系。采集端、9011 及模型配置不用同步。

## 定向验证

```powershell
python -m unittest backend.tests.test_directory_upload_jobs backend.tests.test_upload_sources backend.tests.test_yundao_script_adapter backend.tests.test_internal_dataset_upload backend.tests.test_data_publishing backend.tests.test_pipeline_release -q
cd frontend
npm test -- src/datasetPublishingApi.test.ts src/components/DatasetFolderUploadProgress.test.ts src/views/DataPublishingUpload.test.ts
npm run build
```

这些测试只用临时发布数据及本地模拟脚本，无云道网络传输。内网最终用一个小型发布核对目录层级、选中范围、速度输出及同目标重试。未执行真实上传时不能写成云道验收通过。
