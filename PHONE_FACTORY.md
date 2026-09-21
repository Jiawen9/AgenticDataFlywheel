# 手机工厂接入与本地验收

本轮以 e5ae05c 为基准，在 codex/merge-phone-factory 分支按功能合入采集、监控和模型迭代评估。平台继续运行现有 FastAPI + SQLite，没有引入 Vite 业务中间件、前端 JSON 状态或同事的数据库与设备目录。

## 部署和配置

平台和采集服务是两个进程，磁盘无需共享。先按 [9011 采集服务说明](backend/collector_service/README.md) 部署服务及服务器已有的执行模板，再配置平台进程的环境变量：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| PHONE_FACTORY_SERVER | http://localhost:9011 | 独立采集服务地址 |
| PHONE_FACTORY_TIMEOUT | 30 | 普通 HTTP 请求秒数；平台进程包装器另留 5 秒收尾 |
| PHONE_FACTORY_TRANSFER_TIMEOUT | 300 | 归档／报告下载秒数；平台包装器使用同一配置 |
| PHONE_FACTORY_MAX_ARCHIVE_BYTES | 10737418240 | HTTP 客户端下载字节硬上限；平台还独立校验清单与解压上限 |
| PHONE_FACTORY_TOKEN | 空 | 可选反向代理 Bearer token，采集服务自身不管理用户账户 |

平台启动方式不变。示例（仅配置，不启动任何手机）：

```powershell
$env:PHONE_FACTORY_SERVER = 'http://collector-host:9011'
$env:PHONE_FACTORY_TIMEOUT = '30'
$env:PHONE_FACTORY_TRANSFER_TIMEOUT = '300'
python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

模板 main.py、采集／评估配置、run_evaluator_batch.py、yadb 及其模型依赖没有包含在同事目录中。本仓库提供独立采集服务依赖及可注入的模拟执行适配器；真机使用服务器已有模板。服务返回缺少模板或协议时，界面明确报错。云道 S3 仍使用现有发布接口，真实上传适配器仍待配置。

## 生产采集

1. 在采集页选择生成／扩增批次，或上传现有 17 列 .xlsx 采集任务表。手工表必须保留模板列序，用例编号唯一且可作路径；任务和 App 必填，场景缺失时显示提示并保留未分类。
2. 手工表登记为 manual_collection 业务批次。原件字节、摘要、所有列值与任务分类冻结在 batches/<batch_id>/00_collection/，没有伪造的任务生成作业。上传请求编号保证失败重试复用批次；发布后再次上传可创建新的批次。
3. 选择已关联 App 的手机、VLA、采样参数和经验库开关，可只运行指定手机／App。每次实际配置参与幂等校验；丢失响应后按同一运行编号查询，不能用同一请求编号换参数。
4. 采集服务先持久保存运行，后台执行。运行产物按运行、设备和用例隔离。同名轨迹不会相互覆盖。
5. 平台后台串行拉取完成清单和 ZIP。验证批次、任务、设备、文件路径、清单、大小和 SHA256，暂存后登记到 raw/collection_batches/<batch_id>/runs/<collection_run_id>/。
6. 文件和完成登记都成功后，采集页才显示已回传，预处理页显示可处理轨迹。部分成功保留有效轨迹及错误清单；零有效轨迹不会登记成功。回传失败可以重试同步，不重启手机执行。

采集状态、传输状态及作业记录保存在平台 SQLite；采集服务独立保存其运行和归档。文件替换与数据库提交中断通过恢复日志重试，已完成原始结果不覆盖。已发布批次不再下发或接受迟到写入。当前唯一过程件、修正、发布关闭、外部表发布和 DEV 看板沿用现有链路，分类通过冻结的 00_collection 引用延续。

## 监控与评估

监控页查询实际 ADB 连接、型号、电量、截图和日志。轮询一次完成后再开始下一次；切换设备、隐藏页面、关闭面板或离开页面会取消并使旧响应失效。

评估页复用手机、App、VLA、任务输入和采样配置，使用 modeliter 模式。评估运行及报告与 generate 模式隔离，评估上传本身不创建生产批次或阶段过程件；评估结果不会自动进入训练发布链。报告用运行与文件稳定编号下载，兼容旧文件夹和文件名查询。

删除手机需确认：会中断该手机参与的整个活动运行（包括同一次运行的其他手机），停止对应执行进程后仅移除所选设备工作目录和本地关联。已独立归档的运行结果与报告保留；远端停止或删除失败不会显示成功。

## 实现入口

- 平台路由及持久设置：backend/phone_factory.py
- 平台下发、评估状态及报告代理：backend/phone_factory_runtime.py
- HTTP 客户端：backend/phonefactory_client.py
- 手工采集登记：backend/manual_collection.py
- 结果下载、验证及恢复：backend/collection_transfer.py
- 原始结果完成登记：backend/collection_runs.py
- 独立服务：backend/phonefactory_manager.py、backend/collector_service/
- 三块前端：frontend/src/views/PhoneFactoryCollectionView.vue、PhoneFactoryView.vue、ModelIterationEvaluationView.vue

主要平台接口：

| 接口 | 用途 |
| --- | --- |
| /api/phone-factory/batches | 统一查询未发布的生成、扩增、手工批次 |
| /api/phone-factory/batches/{id}/workbook | 下载冻结采集表 |
| POST /api/phone-factory/tasks | 上传并登记手工批次，返回 imported_task |
| POST /api/phone-factory/remote/start-run | 具名配置下发生产采集 |
| GET /api/phone-factory/collection-runs | 运行及回传状态 |
| POST /api/phone-factory/collection-runs/{id}/sync | 重试拉取并完成登记 |
| /api/phone-factory/remote/status、adb-devices、monitor、del-phone | 设备状态、监控及删除 |
| /api/model-iter/remote/start-run、runs、reports、report-download | 独立评估与报告 |

## 模拟验收

后端测试统一使用临时目录、临时数据库、模拟模型和设备。完整 HTTP 协议测试包括真实客户端 multipart 序列化、模拟采集服务、双手机归档下载、生成／手工批次完成登记、预处理可见、评估隔离与报告下载：

```powershell
python -m unittest backend.tests.test_phone_factory_integration -v
python -m unittest backend.tests.test_phone_factory_runtime backend.tests.test_collector_service backend.tests.test_manual_collection backend.tests.test_collection_transfer backend.tests.test_collection_runs backend.tests.test_phone_factory backend.tests.test_preprocessing_jobs
npm --prefix frontend test
npm --prefix frontend run build
```

浏览器模拟脚本 frontend/tests/phone-factory.browser.cjs 覆盖三页操作、运行参数、失败重试、回传、设备切换、删除失败与成功、评估报告；batch-retirement.browser.cjs 检查发布后的页面清理。需要已有 Playwright 与浏览器运行环境，脚本通过请求拦截提供模拟响应，不连接真实服务。

真机验收仍需在采集服务器完成：核对执行模板的输出目录与用例编号，验证 ADB、模型端点、多手机采集与完整回传。不得将 PID 消失或设备空闲作为成功证据。

## 本次验收记录（2026-09-21）

- 完整后端测试 554 项通过，覆盖采集、预处理、修正、批次关闭、发布、外部表、DEV 统计及 S3 模拟接口。
- 前端 268 项测试（37 个测试文件）、TypeScript 检查与 Vite 构建通过。
- 手机工厂 11 项模拟浏览器操作通过；预处理在 1440/1024/390 三种宽度通过；批次发布关闭与缓存回归通过。
- 对照实施前 SHA256，数据库、批次过程件、原始轨迹、发布及看板数据共 3103 个文件无变化，也未新增或移除。
- 同步修复回归发现的现有外部表预览缺少 deepcopy 导入问题；未更改发布统计口径。
- 本地记录保存在 backend_workspace/tmp/phone-factory-merge/，没有提交或推送 Git，也没有进行真实手机、模型或上传调用。
