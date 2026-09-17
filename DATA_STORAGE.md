# 统一数据存储与阶段产物

本方案覆盖任务生成、扩增、手机采集登记、轨迹转换、标框、Observation/中间态判断、建树、质检、修正、COT 和数据发布。当前运行数据统一读写项目根目录的 `backend_workspace/`，可通过 `ADF_DATA_ROOT` 指定其他根目录。该目录沿用此前 `data/` 的完整内部结构；原旧 workspace 的模块目录和历史记录不合并、不导入。后端也不扫描 `backend/_workspace/` 或 `frontend/data/`。

## 三种格式的职责

| 形式 | 用途 | 更新方式 |
| --- | --- | --- |
| SQLite：`backend_workspace/system/app.sqlite` | 当前作业状态、人工编辑、结果、批次索引和发布登记 | 后端事务更新；需要整记录保存时校验版本 |
| JSON | 模块之间读取的结构化数据，以及各阶段的完整版本 | 每次完成阶段保存新版本；下游读取 JSON |
| Excel | 人工查看、核对、下载和对外交付 | 与阶段 JSON 一起保存为快照；不反向导入手改内容 |

SQLite 只需一个本地数据库文件，无需单独安装数据库服务器。多个用户通过同一个后端访问它。当前后台作业仍按单个后端进程管理，启动时使用一个 worker；本次没有引入分布式作业调度。

流程间必需的 JSON 缺失、损坏或校验失败时明确报错，不回退 Excel、旧目录或其他批次，也不靠重新调用模型补齐来掩盖输入缺失。Excel 被修改不会覆盖当前存值。手动修改请通过页面保存；知识库、扩增失败用例和采集任务仍接受各自入口明确上传的 Excel，这些是输入操作，不是对阶段核对表的修改回导。

JSON 可避免下游反复解析 Excel，SQLite 可减少并发读改写丢失；模型推理和图片处理的耗时不因此消失。本次没有声称固定比例的性能提升。

## 目录

```text
backend_workspace/
├─ system/
│  ├─ app.sqlite                  当前状态、人工编辑和索引
│  ├─ preprocessing/             当前预处理 JSON、Excel 和输入登记
│  ├─ task_generation/           作业输入、知识库快照、导出和冻结采集批次
│  ├─ trajectory_tree_runs/      按 run_id 保存的建树运行文件
│  ├─ trajectory_quality_results/  质检运行文件
│  └─ trajectory_correction/     冻结会话 JSON/Excel 输入和导出
├─ raw/
│  ├─ rollout_trajectories/     显式导入的原始轨迹、截图和 XML
│  └─ collection_batches/      按业务批次与 collection_run_id 保存的采集原件
├─ inputs/phone_factory/        手机采集上传表
├─ resources/task_generation/KnowledgeBase/  场景树与先验资源
├─ batches/<batch_id>/<stage>/<version>/
│  ├─ result.json               这一版完整结构化结果
│  ├─ result.xlsx               这一版人工核对表，具体名称见 manifest
│  └─ manifest.json             上游关系、文件名、SHA256、时间和版本
├─ releases/<release_id>/
│  ├─ manifest.json
│  ├─ 001/<原文件名>.xlsx       发布时冻结的第一份表
│  └─ 002/<原文件名>.xlsx       多会话发布时保留多份表
├─ cache/                       可复用的模型/标框缓存
├─ logs/                        日志
└─ tmp/                         尚未完成发布的临时文件
```

作业状态、人工编辑和当前结果只保存到 SQLite，不再双写草稿、结果和作业状态 JSON，也不会用磁盘旧 JSON 恢复数据库记录。`system/` 中的文件用于当前流程的冻结输入、诊断和导出；查看已完成阶段应使用 `batches/` 中的版本。文件清单以对应 `manifest.json` 为准：已有导出器的阶段可能保留原 Excel 文件名，建树阶段主要保存 JSON。

一个阶段完整写好 JSON、Excel 和清单后才登记到索引。失败不会登记一份只有部分文件的阶段版本。每次重新保存阶段都会生成新版本，已有版本不覆盖。

## 逐阶段保存什么

| 阶段目录 | 产物 | 保存时机 |
| --- | --- | --- |
| `00_task_generation` | 普通生成当前结果 JSON/Excel | 本次生成结束 |
| `00_scene_matching` | 扩增源失败用例、分类和错误 JSON/Excel | 分类阶段结束 |
| `00_augmentation` | 扩增结果 JSON/Excel | 扩增结束 |
| `00_collection` | 冻结采集输入 JSON 和原 17 列采集表 | 首次提交采集 |
| `00_task_export` | 本次导出的未删除任务 JSON/原格式 Excel | 点击生成侧导出 |
| `01_conversion` | 转换后的步骤 JSON/轨迹 Excel | 原始轨迹转换结束 |
| `02_annotation` | 步骤、动作框 JSON/标框 Excel | 标框结束或页面保存标框 |
| `03_observation` | 全部步骤的 Observation、中间态类别、判断原因及是否计入树 | 建树任务发布时，保存同次视觉判断的全部步骤 |
| `04_tree` | JSON 内含轨迹树和同版本的完整质检输入 | 建树完成 |
| `05_quality` | 评分 JSON，轨迹汇总、步骤评分、评分标准 Excel | 质检成功 |
| `06_correction` | 完整步骤 JSON/Excel，含人工编辑、删除和导出选择 | 修正导出、完整导出或提交 COT 前 |
| `07_cot` | 完整过程 JSON/Excel，另附原完整数据集 Excel | COT 完成或完整数据集导出 |
| `releases/<release_id>` | 发布清单和所选完整数据集 Excel 的冻结副本 | 创建数据发布记录 |

普通编辑立即保存当前状态，不会为每次按键或字段保存生成一套 Excel。生成侧额外提供 `POST /api/task-generation/jobs/{job_id}/snapshot`，可从已保存结果补存中间表，不调用模型。COT 结果已保存但过程表失败时可重新导出，不必重新生成。

`06/07` 的完整过程表保留已删除步骤，便于追溯；对外 SFT/RL 和完整数据集导出继续遵循现有各自规则，不能把过程表当成训练投影。Observation 阶段同样保留未计入树的广告、加载等步骤。

单一上游批次继续沿用其 `batch_id`；修正与 COT 沿用冻结的建树批次关系。采集批次号等于来源生成作业 ID；每次采集使用独立 `collection_run_id`，按 [采集完成协议](backend/COLLECTION_RUNS.md) 登记原文件，并在同批次发布01／02。前端按批次和标框版本提交建树，避免全局结果覆盖。完整入口见 [批次预处理](backend/PREPROCESSING.md)。

`run_jiawen.py export` 辅助命令保存工作簿和 JSON 侧车，不登记为完整的广告分类阶段。网页建树保存的 `03_observation` 包含同步视觉分类结果。

## 使用方法

先将需要重新处理的原始任务目录显式复制到 `backend_workspace/raw/rollout_trajectories/`，保留任务／轨迹层级、截图、XML 和响应文件。确认同名任务不会覆盖已有输入后再复制。复制原始输入不包含旧转换表、标框结果、作业记录、评分或缓存；后端不会从旧根目录自动寻找缺少的文件。

从项目根目录运行。默认不需要配置路径：

```powershell
# 新原始轨迹放到 backend_workspace/raw/rollout_trajectories 后，转换并运行标框
python backend/trajectories_preprocessing.py --batch-id processing-demo-001

# 明确指定本次已复制的新目录
python backend/trajectories_preprocessing.py --source "backend_workspace/raw/rollout_trajectories" --batch-id processing-demo-001

# 只做格式转换，不调用标框模型
python backend/export_vla_trajectories.py "backend_workspace/raw/rollout_trajectories" --batch-id processing-demo-001
```

前两个命令会调用现有标框模型，仍需原模型配置。随后在页面提交建树、质检、修正、COT 和发布，阶段产物自动保存。重复使用同一个 `--batch-id` 会增加该批次的阶段版本，不覆盖之前的表格。

需要把所有新数据放在另一块磁盘时，在**启动后端或脚本前**设置进程环境变量：

```powershell
$env:ADF_DATA_ROOT = "E:/AgenticData"
python -m uvicorn backend.api:app --host 0.0.0.0 --port 8765 --workers 1
```

`ADF_DATA_ROOT` 在 Python 导入存储模块时读取，不通过模型配置文件 `backend/.env` 延迟加载。预处理/转换 CLI 也支持 `--data-root`；网页服务必须指向同一根目录，才能看到同一份数据。指定其他根目录时，原始输入也应复制到该根目录的 `raw/rollout_trajectories/`。

知识库等必要资源由操作者明确放入 `backend_workspace/resources/`，或通过对应页面上传，不自动复制旧知识库。需要保留既有场景树节点身份时，显式复制完整知识库版本目录及其 `current.json`；不要把旧作业、结果或采集任务一起复制进运行目录。旧发布记录不会自动出现在新页面，也不会覆盖旧发布文件。

## 手机设备和配置的一次性准备

手机采集的 `/api/phone-factory/*` 统一由 FastAPI 提供，开发模式通过 Vite 的 `/api` 代理访问。没有 SQLite 状态时，页面返回空设备／任务列表和默认参数，不从 `frontend/data/` 导入。上传幂等规则、运行按钮和 Python 手机协议保持不变。

需要沿用已确认的设备配置时，可在后端 Python 中显式调用：

```python
from backend.phone_factory import PhoneFactoryStore

PhoneFactoryStore().initialize_settings({
    "phones": ["phone-id"],
    "apps": ["App"],
    "phoneApps": [{"phone_id": "phone-id", "app": "App"}],
    "vla": [],
    "config": {"sampling_enabled": False, "temperature": 0.7,
               "top_p": 0.85, "use_experience_lib": False},
})
```

示例值需替换为已确认的本地配置。方法只接受 `phones/apps/phoneApps/vla/config`，拒绝 `tasks` 和运行记录；关联状态设为“空闲”。已有 SQLite 状态时拒绝覆盖。该方法不读取旧文件、不复制上传表、不下发手机任务，且不是启动时自动执行的步骤。

## 统一查看接口

| 接口 | 返回 |
| --- | --- |
| `GET /api/data-storage` | 实际数据根目录、数据库位置和快照规则 |
| `GET /api/data-batches` | 已登记批次、阶段、版本数和更新时间 |
| `GET /api/data-batches/{batch_id}/artifacts` | 该批次全部已保存版本 |
| `GET /api/data-batches/{batch_id}/artifacts/{stage}/{version}` | 单一版本清单 |
| `GET /api/data-batches/{batch_id}/artifacts/{stage}/{version}/files/{filename}` | 下载登记文件，验证 SHA256 |

缺少文件/版本返回 404；文件被篡改等完整性错误返回 409。读接口不生成模型结果、不临时补 Excel、不迁移数据。现有业务 API 地址保留，内部数据源已经切换；去掉的是旧文件读取链路，不是页面仍在调用的业务路由。

生成导出仍调用 `POST /api/task-generation/jobs/{job_id}/export`，修正导出仍调用 `POST /api/correction/sessions/{session_id}/export`，完整数据集导出仍调用同会话下的 `/dataset-export`；下载也沿用响应中的业务地址。后端将文件保存到新 `backend_workspace/system/` 导出目录，并保存对应阶段快照。统一产物接口用于查看、核对和下载阶段文件，不能将修正过程表误当成 SFT/RL 导出。发布 Excel 从 `backend_workspace/releases/` 的冻结副本下载。

浏览器点击下载后的本机副本仍使用浏览器的下载目录；这里的 `backend_workspace/` 指后端已保存的产物位置。

## 本次重跑验收

本次使用批次 `validation-20260915-155706`，输入任务为 `AT-YYSP-AQY-001`，基线为 10 条轨迹、142 个 response。该批次已完成转换、标框、Observation、建树和质检；实际阶段版本以已登记的 manifest 为准，未自动进入修正、COT、发布或上传。

1. 在首次重跑时显式复制原始任务并核对文件清单与哈希；此次目录更名保留这份已核对的原始数据，原旧 workspace 随迁移验收完成后删除。
2. 重新执行转换和标框，核对 `01_conversion`、`02_annotation` 的步骤身份、数量、动作和框。
3. 在页面建树，检查 `03_observation` 保留全部步骤及广告／中间态原因，`04_tree` 保存树和同版本质检输入；未入树步骤不能从过程表消失。
4. 执行质检并检查 `05_quality` 的 JSON 和评分表可追溯到同批次轨迹。到此暂停，由用户核对，不自动继续修正、COT、发布或上传。
5. 隔离测试验证：旧目录哨兵记录不可查询；Excel 改动不改变内部读取结果；必需 JSON 缺失或损坏时明确失败；阶段文件哈希与清单一致。

## 备份与范围

停掉后端、预处理和其他写入脚本后，备份整个 `backend_workspace/`。只备份 SQLite 会缺少图片和 Excel，只备份 Excel 会缺少当前编辑和索引。更换数据根目录应复制完整目录并核对上游资源可访问，不要在多个进程运行时手工覆盖数据库。迁移中的旧目录仅作临时回退，验收成功后由迁移命令删除；不作为新系统的数据来源。

本次仅清理用户指定的原旧 workspace；当前统一目录内的历史阶段和冻结文件全部保留。不调整训练配比/模型发布等页面中的演示存储，也不新增 Excel 修改回导或依赖调度。采集端需按完成登记协议将结果写到共享数据目录；未接通时页面明确等待采集结果。

## 从 data 更名到 backend_workspace

一次性迁移命令先只读检查，再停止写入并正式切换。旧目标目录临时保存在项目内 `.data-root-migration/`，不合并到新数据。迁移记录、校验清单和原 SQLite 备份保存在同目录，供故障恢复与审计。

```powershell
python -m backend.data_store.migrate_root check --project-root .
# 确认无运行中作业，停止后端及其他写入脚本后执行
python -m backend.data_store.migrate_root apply --project-root .
# 检查迁移后文件字节、登记路径与阶段文件完整性
python -m backend.data_store.migrate_root verify --project-root .
# 完成文件校验、页面及接口验收后删除暂存的原旧 workspace
python -m backend.data_store.migrate_root finalize --project-root .
# 如尚未 finalize 且验收失败，停止写入后恢复
python -m backend.data_store.migrate_root rollback --project-root .
```

冻结 JSON、Excel、原始轨迹与缓存原字节保持不变，不批量替换其中记录的旧绝对路径。SQLite 保存明确的原根目录登记，路径解析器只将已登记旧根的相对后缀定位到当前数据根，并校验路径边界；不会访问旧目录、创建目录联接或通过扫描补数据。因此历史 JSON 内可能仍显示当时的原路径，这是来源记录，不是当前读取位置。

迁移事务会重建以工作簿绝对路径为键的标框索引，保留人工存值和阶段版本。当前 API 地址、`ADF_DATA_ROOT` 变量和 `@data/` 标记不改名。后端重启必须使用新根；不能沿用指向原 `data` 的启动环境。

迁移命令使用操作系统文件锁，进程退出后锁自动释放，可按迁移记录继续执行。正式删除旧目录前可回退，但若切换后已经新增或修改业务记录，回退会拒绝覆盖数据库；应先保留新写入并人工制定恢复方案。迁移审计目录和原 SQLite 备份不参与业务读取，建议随运维备份保留。

业务全流程与存储关系图见 [Word 设计文档](outputs/数据流转与存储设计.docx)。
