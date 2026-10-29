# 手机工厂接入与本地验收

双引擎增量：采集页默认配置、手机/App 定制运行和自动 Pipeline 的采集配置均可选择 NewAgent 或 PipelineAgent。选择通过请求顶层 `agent_type`（`newagent` / `pipelineagent`）传到同一台采集电脑的 9011 服务；未传该字段的旧调用仍使用 NewAgent。`GET /api/phone-factory/agent-capabilities`（评估入口同样支持）返回电脑声明的 `agent_types` 能力，未部署或不支持 Logprobs/模型迭代评测的引擎会在下发前被拒绝。规划、反思等辅助模型继续由采集电脑配置。

`/config` 增加保存和返回默认 `agent_type`；运行时 `config` 仍只包含原采样、经验库和 Logprobs 参数，引擎字段独立冻结并参与幂等校验。自动 Pipeline 的旧冻结配置缺失此字段时保持 NewAgent，历史请求和归档摘要不重写。生产 `generate` 结果仍进入预处理/质检/发布，`modeliter` 仍是独立的边采边评入口。

PipelineAgent 轨迹保留原始模型 `content` 与 Logprobs；规范化导出的 `action` 必须来自已记录的 `executed_action`，坐标系标为 `pixel`，并逐步与 `_trajectory_for_evaluate.json` 的 `actions_flat` 核对一致。模型坐标的 `parsed_action` 仅保留作诊断，原文不可解析时只允许明确 `parsed`/`repaired` 证据支持的摘要回退。预处理优先使用 `stepNNN_vla_input_stability_ui.xml`，再使用原输入 XML 与既有 done XML 回退。引擎、版本、实际运行代码/配置摘要保留于清单和下游 JSON 来源元数据，业务 Excel 列保持原有格式。

生产运行列表的“停止”调用 `POST /api/phone-factory/collection-runs/{id}/cancel`；只有采集电脑确认相同运行已取消后才记录中断，已有文件继续保留。自动 Pipeline 管理的批次仍由流程页面控制。

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
2. 手工表登记为 manual_collection 任务来源。原件字节、摘要、所有列值与任务分类冻结在 batches/<batch_id>/00_collection/，没有伪造的任务生成作业。上传请求编号保证失败重试复用来源；同一来源可供不同引擎或不同轮次反复采集。
3. 在手机／App 行点击“定制运行”，弹窗固定该手机和 App，再选择任务、VLA、采样参数和经验库。本次配置不会改动全局配置。任务可以选择已提交但尚未登记到手机工厂的生成／扩增批次，也可以选择手工上传批次；评估页只显示评估任务。每次实际配置参与幂等校验；丢失响应后按同一运行编号查询，不能用同一请求编号换参数。
4. 平台为每次新采集先创建独立结果批次（batch_role=collection_result），冻结本次手机/App 实际匹配的用例及全部17列，再登记一次采集运行。task_source_batch_id 指向可复用的原任务来源，task_id、用例编号和来源分类保持不变。采集服务持久保存该运行后后台执行；同名轨迹按批次、运行、设备和用例隔离。
5. 平台后台串行拉取完成清单和 ZIP。验证批次、任务、设备、文件路径、清单、大小和 SHA256，暂存后登记到 raw/collection_batches/<batch_id>/runs/<collection_run_id>/。
6. 文件和完成登记都成功后，采集页才显示已回传，预处理页显示可处理轨迹。部分成功保留有效轨迹及错误清单；零有效轨迹不会登记成功。回传失败可以重试同步，不重启手机执行。

新采集响应中的 `batch_id` 是结果批次，`collection_run_id` 标识本次下发，`task_source_batch_id` 标识任务来源。同一来源每次真正重新采集都会得到新结果批次，即使用同一引擎也不追加到旧结果；一次下发中的多手机仍属于一个结果批次。相同请求编号的网络重试复用原批次和运行，更改引擎或参数则冲突。原始结果、预处理、标框、建树、质检和发布全部按结果批次隔离。

手机占用仍按物理设备判断；同一手机的不同 App、生产和评估之间互斥。“开始运行”向所有匹配手机下发，命中忙设备则整次拒绝，不默默跳过。页面按任务来源列出各次结果，预处理入口使用所选运行的结果批次编号，不能使用任务来源编号。

自动流程从采集开始时先预留结果批次，后续下发复用这个批次；从已有结果继续时不新建。发布结果只关闭自身，任务来源仍可只读复用，包括拥有历史已发布结果的来源。结果快照和工作簿独立保存，后续不依赖来源目录。历史混合批次保持原目录和原处理范围，不自动拆分或重写；历史请求重放不会转成新批次。

新结果采集要求 9011 声明 `protocol_version: 1` 和 `batch_results: true`，并支持所选引擎。能力检查失败时保留本次批次和请求供重试，不退回省略批次／运行编号的旧协议。已有历史运行继续按其冻结的传输方式查询和恢复。

采集状态、传输状态及作业记录保存在平台 SQLite；采集服务独立保存其运行和归档。文件替换与数据库提交中断通过恢复日志重试，已完成原始结果不覆盖。已发布结果批次不再下发或接受迟到写入，完整任务来源可只读复用。当前唯一过程件、修正、发布关闭、外部表发布和 DEV 看板沿用现有链路，分类通过冻结的 00_collection 引用延续。

本次上线同步更新平台后端和创新平台内嵌的前端模块，不修改 9011 或两种执行引擎。停止新下发后更新并重启平台；保留当前数据库、数据目录和原代码，勿拆分或搬迁历史混合批次。回滚前停止新版本活动流程并保存新增结果数据，不用旧版本向新增结果批次追加运行。

## 监控与评估

监控页显示已连接手机、全部手机、采集任务数，以及设备型号、关联 App、电量、截图和日志。设备列表约 10 秒刷新，打开的详情约 1 秒刷新，各自串行；详情可独立刷新，日志追加后滚动到底部。切换设备、隐藏页面、关闭面板或离开页面会取消并使旧响应失效。

评估页复用手机、App、VLA、任务输入和采样配置，使用 modeliter 模式。评估运行及报告与 generate 模式隔离，评估上传本身不创建生产批次或阶段过程件；评估结果不会自动进入训练发布链。报告区支持独立刷新、轮次与文件时间、空轮次提示。报告用运行与文件稳定编号下载，保留原文件名，兼容旧文件夹和文件名查询。

删除手机需确认：会中断该手机参与的整个活动运行（包括同一次运行的其他手机），停止对应执行进程后仅移除所选设备工作目录和本地关联。已独立归档的运行结果与报告保留；远端停止或删除失败不会显示成功。

## 实现入口

- 平台路由及持久设置：backend/phone_factory.py
- 平台下发、评估状态及报告代理：backend/phone_factory_runtime.py
- HTTP 客户端：backend/phonefactory_client.py
- 手工采集登记：backend/manual_collection.py
- 每次下发的独立结果批次及17列冻结：backend/collection_result_batches.py
- 结果下载、验证及恢复：backend/collection_transfer.py
- 原始结果完成登记：backend/collection_runs.py
- 独立服务：backend/phonefactory_manager.py、backend/collector_service/
- 三块前端：frontend/src/views/PhoneFactoryCollectionView.vue、PhoneFactoryView.vue、ModelIterationEvaluationView.vue

主要平台接口：

| 接口 | 用途 |
| --- | --- |
| /api/phone-factory/batches | 查询任务来源；batch_role 可选 source／collection_result／all，include_published=true 包含已发布项 |
| /api/phone-factory/batches/{id}/workbook | 下载冻结采集表 |
| POST /api/phone-factory/tasks | 上传并登记手工批次，返回 imported_task |
| POST /api/phone-factory/remote/start-run | 具名配置下发生产采集 |
| GET /api/phone-factory/collection-runs | 运行及回传状态；task_source_batch_id 筛选同一任务来源的各次结果 |
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

## 功能恢复验收（2026-09-22）

- 恢复手机／App 行的定制运行及任务选择，保留批次区；同批次可以向另一台空闲手机下发。批次区和定制草稿可指向不同批次，发布通知或焦点补查只清理对应批次。
- 恢复监控设备/App/任务统计、独立刷新与日志滚动，以及评估报告刷新、轮次/文件时间和空轮次提示。
- 191 项相关后端测试通过：49 项运行/采集服务/完成登记，44 项 HTTP 集成/回传/手工批次/预处理，以及 98 项批次关闭/发布/外部导入/看板/S3 回归。
- 前端全量 280 项测试（38 个测试文件）、TypeScript 检查与 Vite 构建通过。
- 16 组真实 Element 表格的模拟浏览器场景通过，102 次严格模拟 API 调用，无页面错误或外部请求。另通过 5 个处理页面的发布关闭回归，含发布失败保留、迟到响应、焦点恢复及 S3 恢复信息保留。
- 对照实施前 SHA256，批次、原始数据、发布、系统数据库及输入共 3121 个文件无变化，也未新增或移除。验收记录位于 backend_workspace/tmp/phone-factory-restore/；未调用真实手机、模型或上传服务。
