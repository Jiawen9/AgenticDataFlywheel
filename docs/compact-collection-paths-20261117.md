# 新采集运行的本地短目录

本轮只为新建的 HTTP 回传运行与新接入的续采执行段登记 `compact-v1` 物理布局。运行编号、批次编号、用例编号、重复序号、源轨迹编号和请求指纹不变；旧运行重放不补映射、不搬原件，不迁移数据根。

平台 SQLite 的 `collection_storage_layouts` 保存运行物理目录，`collection_evidence_locations` 保存轨迹或诊断的目录。两者通过现有唯一编号分配器预留名称，并保存完整来源身份和映射摘要；不会只截断 hash 后假定无冲突。

```text
<DATA_ROOT>/raw/collection_batches/<batch>/runs/rYYMMDD-NNN/trYYMMDD-NNN/<原文件及子目录>
<DATA_ROOT>/raw/collection_batches/<batch>/diagnostics/rYYMMDD-NNN/dYYMMDD-NNN/<诊断文件>
<DATA_ROOT>/tmp/collection_transfers/rYYMMDD-NNN/a/trYYMMDD-NNN/files/trYYMMDD-NNN/<原文件>
```

原运输清单、单轨迹 ZIP、最终清单及 SHA256 保留。解压先校验 ZIP 内的原路径、声明文件列表、大小及摘要，再直接写入登记的短位置；临时文件不会先落到长路径。回传重试沿原身份幂等校验，丢失确认时复用已经保存的原件。

运行记录的 `trajectories` 仍描述运输来源，不能用其 `relative_dir` 拼接 `output_dir` 推断新物理目录。后台统一通过 `collection_entry`/`run_root` 解析；原件下载接口继续接受原清单文件路径。提交给预处理的冻结输入包含实际 `storage_run_dir`、本地 `relative_dir/files` 和 `source_relative_dir/source_path`。转换后的 `image/xml/logprobs_path` 已是可直接访问的本地相对路径，业务轨迹身份仍从原来源生成。树、坐标、纠偏、评分和发布使用冻结的 `raw_root + image`，无需知道短目录分配方式。

旧目录、旧快照和旧发布引用保持原样。混合旧运行与新续采段的输入逐轨迹使用各自的物理位置；已封存结果不会因另一执行段采用短目录而改写。映射不可读或身份不一致时保留具体错误，不猜测来源或重建任意历史目录。

缩短的是系统生成的运行、轨迹及临时层级，原始文件名和子目录不改。外部深层文件仍可能超过 Windows 传统长度，因此实际读写、ZIP、摘要和原件下载继续使用 `file_io.io_path`。不新增基于总长度的接收门禁，也不宣称第三方上传脚本已经因此支持所有长路径。

新建物理映射附带 `storage_path_budget`：用固定轨迹文件名样例估算 Windows UTF-16 路径长度，目标 240，超过时记录 `generated_path_exceeds_budget`，不阻断接收。它只属于本地存储诊断，不进入请求指纹、远端清单或模型配置；已经存在的映射不会补写。此样例不能代替对任意外部深层文件名的长路径支持。

## 定向验证

- `backend.tests.test_compact_collection_paths`：8 项通过。覆盖 NewAgent/Pipeline × v1/v2 的真实 ZIP 解压、先到单条、幂等接收、原件下载、预处理转换、树与质检输入、坐标读取；并发唯一映射、身份冲突、旧运行重放、旧/新输入混合、独立续采物理位置、路径预算诊断与 ZIP 越界检查。
- 回传、接收处置、续采、预处理、短编号相关 6 个模块共 89 项通过。历史测试中 3 处直接拼旧物理目录的断言改为使用登记位置；长于 300 字符的原始嵌套文件仍按原摘要验证。
- 独立兼容测试 `collection_runs/rollout_imports/short_ids/downstream_root_relocation` 的原件/导入部分通过。发布上传适配测试中 Mock 配置问题已交由对应模块修复；完整交付以总验证记录为准。
- 测试仅使用临时目录、模拟采集端与模拟模型，没有访问真实采集服务、实际模型或内网数据；现场上传脚本、旧批次和真实设备验收仍需单列。
