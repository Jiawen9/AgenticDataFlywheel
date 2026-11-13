# 平台本地 Checklist 迁移验证

日期：2026-11-13。范围是轨迹质检、其部分结果与坐标展示；未修改采集服务、模型迭代评估、手机任务分配或运行数据。用户后来确认旧质检最终出结果，因此不能把此前现场现象断言为永久死锁。以下故障修复来自代码审查和离线故障注入，现场慢点仍需结合真实阶段日志判断。

## 验证结果

后端共 **194 个不同定向用例，最终均通过，无跳过**：集中运行 193 项后发现一项快速子进程退出时请求事件未排空的问题，修复并增加确定性用例；随后本地执行器及真实 runner 链路 20/20 复测通过。机器记录为同目录 VALIDATION.json，保留集中运行和复测的分别结果。前端定向 67/67 通过；类型检查和 Vite 构建通过。完整前端 595/598 通过，其余三个在未修改基线也同样失败，位于 ModelIterationPanel.test.ts：迟到运行响应、独立评测取消确认、准备暂停重试。本轮未改模型迭代生产代码，不能把完整前端测试写成全通过。

后端使用 Windows / Python 3.12.13 和现有平台虚拟环境，真实启动隔离子进程，模型由本机离线替身响应。不调用现场模型、不操作手机。第一次集中回归的三个失败来自测试启动目录及长路径夹具不足 260 字符，修正测试入口和按实际路径动态生成超过 280 字符的夹具后重跑；未用放宽断言代替长路径验证。

本机实际依赖预检返回 `{"ok": false, "missing": ["cv2"]}`。已提供独立 requirements；本轮没有自动升级原平台或手机环境。部署时须在选定的 ADF_CHECKLIST_PYTHON 环境安装依赖并使预检通过。隔离子进程与模拟视觉模型测试通过，不等于现场 Python 3.11 完整依赖、真实模型或网关已经验收。

## 覆盖范围

- 两种引擎真实预处理转换、建树输入加载、原始 collection_case_id 和验收条件进入 Checklist；平台短号、旧相对路径、Windows 中文长路径。原件、旧工作簿及其摘要保持不变。
- b261112-004 等价的十轨迹恢复演练：继承冻结配置，Rubric 检查点复用，补做 Checklist/F/Q；有效完成的旧评分不主动重评。真实现场批次未由本地测试操作。
- 真实 runner→Checklist 子进程组合：一条成功、一条源目录不可读时部分结果立即可读；修复后只补失败项，不重复评分已验证结果。
- 冻结规则按 ID/摘要导入，失败首次导入不隐藏原规则，自定义目录一致，空密钥环境变量名使用角色默认值；模型配置之后与模型迭代独立。
- SQLite 领取/台账锁故障、跨父进程两个槽位、取消、真实父进程退出、迟到写入、单个未知子进程占用不挡另一个槽位。停止未确认不得覆盖旧子进程身份或继续派发。
- 短输出、大量输出、跨块密钥脱敏、日志开始及中途写入失败、半写小型描述恢复；日志失效不造成输出管道无人消费。
- 缺 verdict、损坏或不完整报告不得作为有效缓存；同次重试生成新尝试，保留旧诊断。全部技术失败不额外伪造定义不一致。
- 判定需要的视觉检查仍执行；quality_only_v1 不进行结束后的重复视觉补扫和额外文本总结。
- 分阶段计数、部分评分、整批不完整明确失败、不提前推进自动发布；原评分公式与节点 Q 均值保持一致。
- 坐标使用原 tree GET 路由的 view=coordinate，绑定任务/步骤/版本；根节点、不显示标点和无坐标动作不请求，失败手动重试，切换时丢弃旧响应，放大复用。

## 后端回归入口

在平台项目根目录，选用平台测试依赖完整的 Python，并把项目根及 backend/DevelopRubrics 加入 PYTHONPATH。定向模块如下（不会启动真实模型/手机）：

```text
backend.tests.test_quality_fusion
backend.tests.test_quality_process_supervision
backend.tests.test_batch_results
backend.tests.test_quality_frozen_retry
backend.tests.test_pipeline_quality_fusion
backend.tests.test_quality_source_recovery
backend.tests.test_quality_execution
backend.tests.test_quality_deadline_pipeline
backend.tests.test_quality_control_compatibility
backend.tests.test_quality_retry_errors
backend.tests.test_quality_local_runner
backend.tests.test_quality_checklist
backend.tests.test_quality_local_settings
backend.tests.test_quality_checklist_local
backend.tests.test_coordinate_info
backend.tests.test_batch_api
```

使用 `python -m unittest <模块名...>`。本机受限沙箱中 asyncio socketpair 会挂起，所以离线子进程测试在获准的沙箱外执行；没有借此访问现场服务。全量手机采集验收未执行，也不属于本轮验收。

前端定向文件：qualityScores、coordinateApi、useCoordinateInfo、QualityFusionSettings、ActionImage、useBatchQualityWorkspace、QualityView 的测试；构建命令 `npm run build` 已含 vue-tsc。构建仅有已有的大 chunk 提示。

## 现场待验

1. 平台解释器依赖预检、后端密钥环境变量及实际文本/视觉模型 ID；原内网规则按旧摘要导入。
2. 新质检无需 9011：用已有十条轨迹验证真正 Checklist、F/Q 与覆盖率；已经有效完成的旧结果保留，只有需要补评的作业使用质检重试。
3. icap 原 tree GET 路由实际返回正确坐标 JSON；像素点和 NewAgent 既有约定均核对一条。
4. 核对原件摘要不变、没有轨迹复制、没有重采/重建树；新日志和报告只写平台 system/qc。

部署步骤见 [CODEAGENT_PROMPT.txt](CODEAGENT_PROMPT.txt)，接口适配和配置说明见项目根 QUALITY_FUSION.md。没有采集端增量包，不自动部署或停止任何现场服务。
