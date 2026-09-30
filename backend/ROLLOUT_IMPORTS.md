# 导入已有 Rollout

入口有两处：轨迹预处理与建树页的“导入已有 Rollout”，以及新建 Pipeline 的“接续已有结果”配置区。

## 使用

1. 选择后端电脑上的原始目录，填写新的业务批次编号。默认目录是当前数据根下的 raw/rollout_trajectories。浏览器不上传截图，也不会访问浏览器电脑上的同名目录。
2. 点击“校验预览”，核对每个任务目录对应的任务目标、App／两级场景及轨迹和步骤数量。任务目标缺失时补填后重新校验；App／场景缺失会提示，后续沿用未记录／未分类规则。
3. 点击“确认导入”。后台复制原始字节到新批次独立目录，保存来源清单和任务映射。原目录及已有批次保持不变。
4. 在预处理页点击“开始预处理”，或在 Pipeline 选择该批次并点击“创建并开始”。导入本身不启动预处理、模型或手机。

修改目录、批次编号、分类或任务补充信息后，旧预览失效。预览后源文件改变也必须重新校验。已存在及已发布批次不能作为导入目标；相同导入提交重试复用同一批次。

目录层级为：

```text
原始目录/
  任务目录/
    轨迹目录/
      _trajectory_for_evaluate.json
      turn001_orch_model_request.json
      step001_vla_model_response.json
      step001_vla_input.jpg
      step001_vla_input_ui.xml
      ...
```

任务目录和轨迹目录必须是安全文件名。任务目标优先从原始请求提取，无法确定时由预览确认。步骤须完整，XML兼容既有前一步done XML回退。临时_prefetch_staging目录不作为正式轨迹。错误定位到相对目录，不静默把不完整轨迹当成功导入。

## 后端接入

- GET /api/rollout-imports/options：默认来源和允许读取的目录。
- POST /api/rollout-imports/preview：source_path、batch_id、可选name/app/scene/capability、可选task_overrides；返回import_id、valid、任务/轨迹/步骤计数及warnings/errors。
- POST /api/rollout-imports：import_id、request_id；返回新批次、导入记录编号和计数。重复提交保持request_id不变。
- GET /api/rollout-imports/batches/{batch_id}：活动导入批次的冻结任务来源。

来源路径默认限定在数据根raw下的独立子目录，禁止导入平台管理目录、符号链接或目录联接。额外来源根可通过ADF_ROLLOUT_IMPORT_ROOTS设置，Windows用分号分隔。ADF_ROLLOUT_IMPORT_MAX_FILES默认100000，ADF_ROLLOUT_IMPORT_MAX_BYTES默认20 GiB。预览保留24小时；启动和定期清理仅处理过期且未登记的导入暂存。

导入使用kind/source_kind=rollout_import，00_collection保存任务分类和来源，collection_runs保存已完成的本地导入清单；不创建任务生成作业、任务Excel、手机执行或01–07过程件。collection_run_id仅作为来源关联身份，本地导入没有远端同步过程。原件副本位于raw/collection_batches/<batch_id>/runs/<collection_run_id>/。

文件复制到暂存目录后校验SHA256，再检查源目录是否改变；移入最终运行目录后重新校验文件清单、SHA256和必要轨迹文件，全部通过才登记。00来源、完成清单和幂等记录同事务提交。中断依据导入意图恢复，同一import_id可重试。提交结果不确定时先核对数据库，已登记文件不会被清理。

预处理、建树和发布沿用现有批次及稳定身份规则。分类通过00→01→02来源引用进入发布和看板；导入行为不计为人工精修。一个新批次若最终发布，会产生新发布并累计到总表和看板，使用旧数据验收时应区分测试数据与正式数据。

## Windows 长路径与部署

后端通过 `backend/file_io.py` 在实际文件访问时使用 Windows 扩展路径，包括 UNC 共享路径。数据库、清单、Excel 和接口仍使用普通路径；无需修改注册表或迁移已有数据。轨迹目录内的 `_prefetch_staging` 文件继续完整归档并参与校验，不会为绕过路径错误而删除或过滤。

本次为后端更新，原前端、接口地址、成功响应和同步等待方式保持兼容。部署时更新后端并重启现有服务，无需重新构建或部署前端。如果旧 Pipeline 提示处理代码或配置指纹不匹配，结束旧流程，再选原批次以“接续已有结果”创建流程，无需重新导入。文件确实缺失、摘要不一致或链接越界时仍会拒绝处理，并通过原有错误字段显示轨迹与有限数量的文件名。

导入日志由 `backend.rollout_imports` 以 INFO 级别输出，包含 `import_id`、`phase`、`outcome`、`elapsed_seconds`、`files` 和 `bytes`。阶段包括 `scan`、`copy`、`validate_copy`、`validate_source_after_copy`、`validate_final` 和 `register`；不包含文件正文或模型密钥。需要排查耗时时请在后端日志配置中启用该 logger 的 INFO 级别。

单次扫描只做一遍文件哈希和动作结构解析，复用不跨预览、提交或目录。提交前源文件、复制后副本、复制后源目录和最终目录仍分别进行完整校验，修改时间不能代替 SHA256。8 条模拟轨迹、56 个文件、32 MiB 的本机交替对比中，扫描中位时间从约 0.385 秒降到 0.163 秒；这不是完整导入耗时或部署机性能承诺。同步导入仍可能超过网关等待时间，因此仅此后端优化不能保证消除所有 502。
