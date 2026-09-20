# Agentic Data Flywheel

一个前后端分离的 GUI Agent 轨迹数据飞轮工程，包含场景任务生成、失败任务扩增、轨迹 Excel
导出、动作 bbox 标注、受限中间态过滤、任务级轨迹树构建，以及 Vue 轨迹采集与质检界面。

运行数据统一读写项目根目录的 `backend_workspace/`：SQLite 保存当前状态，JSON 在流程间流转，各阶段保留 Excel 核对表。后端不扫描旧目录、不从旧 JSON 恢复状态；必需 JSON 缺失时明确失败，不回退 Excel。目录、命令、显式资源准备和产物读取接口见[统一数据存储说明](DATA_STORAGE.md)。

## 主要能力

- 递归读取 rollout 轨迹并导出 Excel。
- 为 `click`、`swipe`、`long_press` 动作生成并复核 bbox。
- 使用 Qwen 判断广告、加载、弹窗等临时中间状态。
- 在仅忽略短暂插入状态的前提下构建 action 前缀树。
- 在网页中按真实场景能力树生成、审核和导出任务。
- 上传失败任务并自动匹配场景、扩增变体任务。
- 在网页中浏览任务、轨迹、步骤截图和动作标注。
- 在线修改 bbox，保存当前标注并形成同版本 JSON/Excel 中间产物。
- 批量提交任务建树，在质检页面查看分叉、occurrence 和中间态审计。
- 在网页中直接修正 Action 和步骤，并按 SFT/RL/原生数据分流导出；SOP/COT 由后续模型生成。

## 目录结构

```text
AgenticDataFlywheel/
├─ backend/                       FastAPI、轨迹预处理和建树代码
│  ├─ bounding_box/              bbox 生成与视觉复核
│  ├─ trajectories_tree/         中间态判断和轨迹树构建
│  ├─ task_generation/           任务生成与任务扩增网页模块
│  ├─ trajectory_correction/    轨迹修正网页模块（独立后端包）
│  ├─ tests/                     后端测试
│  ├─ .env.example               模型配置示例
│  └─ api.py                     FastAPI 入口
├─ frontend/                     Vue 3 + Vite 前端
├─ backend_workspace/            统一数据根目录，不提交 Git
│  ├─ system/app.sqlite          当前状态、编辑与产物索引
│  ├─ raw/rollout_trajectories/   原始轨迹放置目录
│  ├─ batches/                   按批次、阶段保存唯一当前 JSON/Excel
│  ├─ releases/                  发布时冻结的 Excel 副本
│  └─ resources/                 场景树和先验知识库
└─ README.md
```

## 1. 获取代码并安装依赖

要求 Python 3.10 或更高版本，以及符合 Vite 7 要求的 Node.js 20.19+ 或
22.12+。

```powershell
git clone https://github.com/Jiawen9/AgenticDataFlywheel.git
cd AgenticDataFlywheel
python -m pip install -r backend\requirements.txt
npm install --prefix frontend
```

## 2. 配置模型服务

复制示例配置：

```powershell
Copy-Item backend\.env.example backend\.env
```

编辑 `backend/.env`：

```dotenv
YUNAI_API_KEY=你的_api_key
MODEL_URL=https://yunai.chat/v1
MODEL_NAME=qwen3.6-27b:floor
COT_MODEL_NAME=qwen3-vl-32b-instruct
```

前三个通用变量是必填项，COT 模型可单独配置：

- `YUNAI_API_KEY`：OpenAI-compatible 模型服务的 API key。
- `MODEL_URL`：模型服务的 base URL。
- `MODEL_NAME`：预处理和建树使用的模型名称。
- `COT_MODEL_NAME`：专家纠偏后生成 Thought/Summary 使用的视觉模型；未配置时默认使用 `qwen3-vl-32b-instruct`，不会改变标框和建树模型。

`backend/.env` 已被 Git 忽略，不要把真实密钥写入 `.env.example`、源码或日志。

## 3. 放置原始轨迹

先将本次要处理的原始任务目录显式复制到下面的路径，确认不会覆盖同名输入。保留完整任务／轨迹层级及截图、XML、响应文件；不要复制旧标框结果、作业记录、质检结果或缓存。后端不会自动查找旧目录。

```text
backend_workspace/raw/rollout_trajectories/
```

预期结构示例：

```text
backend_workspace/raw/rollout_trajectories/
└─ AT-YYSP-AQY-001/
   ├─ AT-YYSP-AQY-001-1/
   │  ├─ step001_vla_input_stability.jpg
   │  ├─ step001_vla_input_ui.xml
   │  └─ ...
   └─ AT-YYSP-AQY-001-2/
      └─ ...
```

原始轨迹、截图、XML、模型请求和响应都属于本地数据，不会上传到 GitHub。

## 4. 预处理轨迹

前端入口为“轨迹采集 → 轨迹预处理与建树”：选择已完成采集登记的批次，点击“开始预处理”，可查看进度、下载初始表、失败重试并继续建树。手机采集端按 [采集完成协议](backend/COLLECTION_RUNS.md) 落盘并登记结果；完整接口及保存规则见 [批次预处理](backend/PREPROCESSING.md)。

在项目根目录运行：

```powershell
python backend\trajectories_preprocessing.py --batch-id processing-demo-001
```

处理流程为：

1. 递归读取正式轨迹并忽略 `_prefetch_staging` 等临时目录。
2. 在 `backend_workspace/system/preprocessing/<批次号>/<执行编号>/` 生成初始表，并保存转换阶段 JSON/Excel 快照。
3. 为目标动作生成候选 bbox，并调用 Qwen 复核。
4. 在同一执行目录生成标框表，并保存标框阶段 JSON/Excel 快照；各批次与执行互不覆盖。

模型响应会写入本地缓存。调用中断后可再次执行命令续跑；任一目标动作标框失败时，
不会发布不完整的标注 Excel。

如需自定义输入、输出或配置文件，可查看全部参数：

```powershell
python backend\trajectories_preprocessing.py --help
```

## 5. 开发模式运行

在第一个 PowerShell 窗口启动后端：

```powershell
python -m uvicorn backend.api:app --reload --host 127.0.0.1 --port 8765
```

在第二个 PowerShell 窗口启动前端：

```powershell
npm run dev --prefix frontend
```

访问地址：

- 前端：`http://localhost:5173`
- 后端：`http://127.0.0.1:8765`
- API 文档：`http://127.0.0.1:8765/docs`

Vite 默认把 `/api` 代理到 `http://127.0.0.1:8765`。

## 6. 网页使用流程

### 任务生成与任务扩增

任务生成页面读取以下本地知识库：

```text
backend_workspace/resources/task_generation/KnowledgeBase/
├─ VLA场景树.xlsx
├─ APP操控先验知识库.xlsx
└─ APP资源先验知识库.xlsx
```

进入“场景能力树”，按 **场景 → 一级能力 → 任务类型** 浏览。点击节点查看详情，勾选节点选择生成范围；App 不再作为树的根节点。任务类型数量按路径去重，App 数量单独计入执行单元。

- 点击“编辑场景树”可新增、重命名、删除三级节点，并按 App 编辑参考示例、资源先验开关。修改统一保存或取消，离开页面前会提示未保存内容；编辑期间不能提交生成。
- 每个选中任务类型默认勾选全部适用 App，可逐项取消。例如一个任务类型选择三个 App、数量设为 5，预计生成 15 条主任务；弱依赖前置任务另计。
- 空场景、空能力和未配置 App 的任务类型可以保存，后者不可生成。缺少操控或资源先验会显示提示，不阻止其他已就绪配置的生成。
- 生成完成后可编辑任务文本、成组删除/恢复弱依赖任务并导出 Excel。已有作业使用提交时的快照，不受后续知识库编辑影响。

首次使用前，显式放入上述三个资源 Excel，或通过页面上传；系统不会从旧目录复制知识库。需要沿用已有节点 UUID 时，可以显式复制完整知识库版本目录及 `current.json`。系统从已提供的资源初始化知识库版本，之后 **`KnowledgeBase/current.json` 指向的 `versions/<版本>/` 才是当前有效知识库**；请通过网页替换文件，不要直接修改根目录原文件或版本目录。
每个版本保存三份 Excel 和 `scene_tree.json`（稳定 UUID 与树结构），通过原子切换版本指针一起发布。改名同步更新操控先验路径；删除节点或移除 App 不删除已有先验记录。旧版本目录完整保留，作为备份。

知识库卡片支持单文件替换和“下载已保存场景树”。下载文件保留六个业务列，并带有隐藏的 `_scene_tree_nodes` sheet，用于保留节点 UUID、空分支及无 App 的任务类型；回传时请保留该 sheet。普通六列表仍支持导入，相同路径复用当前 UUID。不同 App 的示例/资源配置分别保存，同一任务类型/App 出现冲突行时拒绝导入并提示修正。多个标签页同时保存时，旧版本请求返回 `409`，不会覆盖新版本。

场景树接口：`GET /api/task-generation/tree` 返回 `version/scenes/leaf_count/execution_unit_count/warnings`；`PUT` 同路径接收 `base_version/scenes`；`GET /api/task-generation/tree/export` 下载当前 Excel。生成提交采用 `{version, selections: [{node_id, apps}], generate_n}`，不再使用 App 展开的 `node_ids`；业务作业读取接口地址不变，只返回新存储中的记录。

如需无模型费用、无业务数据改动的页面验收，先构建前端，再运行 `python -m backend.tests.scene_tree_demo_server --port 8791`，访问 `http://127.0.0.1:8791/task-generation/scenario-tree`。该验收服务把知识库复制到临时目录，并使用模拟模型；按 Ctrl+C 结束后清理临时数据，不应将其作为正式服务运行。

进入“任务扩增”后上传失败任务 Excel。原始表需要包含 `任务`、`涉及APP`，如果存在 `任务结果` 列则只扩增非 `TRUE` 行；也可以直接上传含有 `app/task/scene/capability/sub_capability` 的 `新场景匹配` 表。扩增结果同样可以审核、删除和导出。

扩增页默认展示紧凑的关联场景树，可切换查看全部场景；变体审核按源失败用例分组，展开后再查看和编辑变体。父组每页 20 条，已删除变体仍可展开恢复。分组、折叠与场景筛选仅影响展示，导出及提交采集始终包含全部未删除结果。

普通生成和扩增均提供 `GET /api/task-generation/jobs/{job_id}/collection-input`，读取当前全部未删除任务及最新人工修改，供采集模块后续接入。字段、依赖校验和响应示例见[统一任务读取接口文档](backend/task_generation/COLLECTION_INPUT.md)。

在生成结果中点击“提交轨迹采集”会冻结当前任务为采集批次，并生成规定的 17 列采集 Excel；手机工厂采集页可选择批次运行。每个作业只提交一份批次，后续源任务编辑不改变已提交内容，原有 Excel 导出保留。批次存储、接口和运行接入方式见[采集批次对接文档](backend/task_generation/COLLECTION_BATCHES.md)。

任务生成状态、结果和源用例保存在 `backend_workspace/system/app.sqlite`，不双写作业／结果 JSON。文件产物保存在：

```text
backend_workspace/system/task_generation/
├─ runs/       # 知识库快照、输入文件和模型调用诊断
├─ exports/    # 业务接口导出的任务 Excel
└─ collection_batches/ # 已提交的采集 JSON 和 17 列 Excel
```

阶段当前过程件保存到 `backend_workspace/batches/<job_id>/`；日志保存到 `backend_workspace/logs/task_generation/`。生成／扩增仍调用原 `/jobs/{job_id}/export` 业务地址，后端保存到新数据目录；浏览器下载副本的位置由浏览器决定。

手机采集仅读取 SQLite 状态和 `backend_workspace/inputs/phone_factory/` 上传文件。没有状态时返回空列表和默认配置。需要一次性保留设备配置时，显式调用 `PhoneFactoryStore().initialize_settings(...)`，仅接受 `phones/apps/phoneApps/vla/config`，不导入任务或运行记录，也不覆盖已有状态；示例见[统一数据存储说明](DATA_STORAGE.md#手机设备和配置的一次性准备)。

如需使用不同模型，可在 `backend/.env` 中设置 `TASK_GENERATION_MODEL_NAME`、`TASK_GENERATION_MODEL_URL` 和 `TASK_GENERATION_API_KEY`；未设置时回退到通用 `MODEL_NAME`、`MODEL_URL` 和 `YUNAI_API_KEY`。并发数使用 `TASK_GENERATION_MAX_CONCURRENT`，默认值为 4。

模型输出处理与排查：

- 默认只接受最终答案，不将 `reasoning_content/reasoning` 当作任务。`finish_reason=length` 会明确报截断；纯 JSON、JSONL、单个 Markdown JSON 块和 `</think>` 后的完整答案仍可读取。混杂分析和多个示例块的响应会要求模型重新输出，不再猜测提取第一个 JSON。
- 生成与扩增统一要求 `{"tasks":[...]}`，每项包含真实 `task`。占位符、空白、非法字段和重复任务不会保存。默认允许一次校验补生成，由 `TASK_GENERATION_VALIDATION_RETRIES` 控制；这是网络重试之外的额外模型调用，设为 `0` 可关闭。数量仍不足时保留有效结果并报部分成功，无有效结果则失败。
- 依赖枚举必须是 `zero/weak/strong` 中的一个；判定失败或弱依赖前置任务无效时，不再伪装成无依赖，该任务不进入可导出结果，错误中说明原因。历史结果不自动清洗或改写。
- 初始/扩增输出预算默认 `TASK_GENERATION_GENERATION_MAX_TOKENS=8192`，依赖与分类默认 `TASK_GENERATION_CLASSIFICATION_MAX_TOKENS=2048`。模型名、地址、密钥保持原配置。第三方推理开关用 `TASK_GENERATION_EXTRA_BODY` 配置，只有确认支持后再填；`TASK_GENERATION_JSON_MODE=true` 同理，不默认强制启用。
- 每次实际请求的 Prompt、参数、模型响应（包含最终内容、推理字段、usage、结束原因）保存在 `runs/<job_id>/model_calls/<trace_id>.json`；错误信息附诊断文件名。非作业直接调用时写入 `logs/model_calls/`。文件原子发布，多个并发请求独立记录，不新增公共下载接口。
- 诊断日志默认开启，可设 `TASK_GENERATION_TRACE_ENABLED=false` 关闭。记录会隐藏配置密钥及常见认证字段，但仍包含业务任务、先验与模型文本，属于本地敏感数据，不应直接上传或分享；可按需手动归档清理。旧作业只有原来的日志，无法补回过去未记录的原始响应。

### 轨迹采集

1. 进入“轨迹采集 → 轨迹预处理与建树”，选择已登记的批次，查看其冻结 JSON 中的任务和轨迹；未登记的目录不会自动出现在页面。
2. 展开任务并选择一条轨迹，按需查看每一步截图、action、summary 和 bbox。
3. 点击截图右上角的“修改 bbox”，重新绘制并保存当前动作框；系统保存当前标注，更新同一份 JSON/Excel 及内部修订号，并只失效该任务的建树及后续结果。
4. 勾选一个或多个已预处理任务并提交建树，等待后台作业完成。

### 轨迹质检

1. 选择一次成功建树形成的时间串任务集。
2. 选择具体任务，查看以桌面为统一起点的 action 前缀树。
3. 点击节点查看 action、summary、bbox、分类信息和所有 occurrence 截图。
4. 在中间态审计中查看未计入树的短暂广告、加载或弹窗步骤。

建树结果保存在：

```text
backend_workspace/system/trajectory_tree_runs/<完成时间串>/
```

作业状态只保存到 SQLite，不再写状态 JSON 镜像。阶段树和 Observation 保存在 `backend_workspace/batches/`。这些都是本地运行产物，
不会提交到 GitHub。

### 轨迹修正

进入“轨迹纠偏 → 专家动作纠偏”，选择已质检批次。当前每个任务仍按原规则选取质检 Top-1；同分时保留原工作簿顺序。已有批次自动恢复原草稿和入选轨迹，不用新推荐覆盖；来源变化的人工修改保留为待复核，采用或放弃后才能继续该任务导出。

页面按“任务行（用例编号）→ 轨迹行 → 修正台”展开，首次进入和刷新默认全部收起。可同时展开多个任务，但整页只展开一个轨迹修正台，展开轨迹才加载步骤与截图。任务统计与轨迹统计分开；前端已预留一任务多轨迹的结构，本次没有开放 Top-3 筛选。

1. 展开轨迹后，左侧查看截图并直接修正 Action，右侧选择步骤；click/long_press 点击图片取点，swipe 按住拖动取起止点，坐标以图片角标显示。
2. Action 点击“保存动作”保存。收起、切换步骤/轨迹/批次、离开页面或导出前保护未保存输入：动作可选择保存、放弃或取消；保存失败保留当前位置和输入。
3. 删除/恢复步骤、加入/取消导出即时保存到草稿。收起轨迹不影响导出开关；“已修改”不代表审核完成。
4. 导出生成 SFT、RL、原生完美通过、原生异常待处理四类工作表；原 Excel 不会被覆盖。批次详情和导出历史默认折叠。

无真实数据和模型调用的浏览器验收：先执行 `npm run build --prefix frontend`，再执行 `python -m backend.tests.correction_demo_server --port 8792`，访问 `http://127.0.0.1:8792/correction/expert-action`。模拟服务提供一个三轨迹任务及一个单轨迹批次，编辑仅驻留内存，重启即丢弃；模拟导出仅用于下载交互检查，实际四类数据分流由后端单元测试覆盖。该服务不是正式后端。

该模块的数据目录为：

```text
backend_workspace/system/trajectory_correction/
├─ inputs/       # 新会话冻结的 JSON/Excel 输入
└─ exports/      # 导出 Excel
```

草稿、人工编辑和 COT 作业状态只保存在 `backend_workspace/system/app.sqlite`，不会从旧会话 JSON 恢复。修正 `/export` 和完整数据集 `/dataset-export` 业务地址不变，导出同时更新对应阶段的唯一过程件。

### 数据发布

进入“专家工作台 → 数据发布”后，可以把 1～N 个尚未发布的纠偏会话登记为一个数据集。发布前，每个会话必须已经通过 COT 工作台右上角的“导出数据集”生成至少一份 `full_dataset` Excel；系统始终选取该会话最新的一份完整导出，不重新生成或合并 Excel。

创建时填写可读的数据集名称，系统生成唯一的 `rel_<随机哈希>` 发布 ID。发布记录保存在：

```text
backend_workspace/system/app.sqlite
backend_workspace/releases/<release_id>/              # 冻结的发布文件
```

记录包含冻结 Excel 的路径、SHA256、行数和上游会话/导出版本关系；发布时复制所选表格，不复制原始轨迹目录。发布成功后批次永久结束处理，前端清理对应批次的选择、草稿、缓存和轮询，刷新及其他标签页也不会重新出现。后端仍保留原始文件、各阶段过程件和人工修改；已发布文件和来源 JSON 独立冻结，下载、统计和 S3 上传继续可用。后续生产使用新的 batch_id。列表和下载只读取当前根目录登记的发布文件，不合并原旧 workspace 的发布记录。

页面下半部分展示新存储中已登记的数据集，支持按名称或发布 ID 搜索、按云道S3上传状态筛选、查看路径与哈希、下载发布 Excel。点击“云道S3上传”由后端按登记顺序读取全部已发布 Excel、核验 SHA256 并调用上传适配器；浏览器只提交发布编号和目标参数。每份回执立即保存，失败后停止，重试跳过已成功文件；刷新可恢复进度，服务重启后可手动重试中断任务。

默认适配器未配置，页面明确提示“云道S3上传尚未配置”并禁用按钮。同事只需完成 [internal_uploader.py](backend/data_publishing/internal_uploader.py) 的配置检查、登录与上传；参数、回执、幂等规则及无网络自测见 [云道S3上传接入文档](backend/data_publishing/INTERNAL_UPLOAD.md)。无需读取编号或自行拼接路径，不上传原始轨迹目录。

不传 target 的既有上传调用继续保留模拟行为：遍历新存储中已登记的发布 Excel 和轨迹根目录、统计进度但不发送文件。模拟状态及以下模拟地址仅在详情中明确标注，不计入云道S3成功数量；这不启用旧目录读取：

```text
s3://training-data/gui-agent-datasets/rel_a84f91c25d3e4b67/
```

模拟目标地址可通过 `backend/.env` 中的 `DATASET_S3_BUCKET` 和 `DATASET_S3_PREFIX` 调整；当前 `DATASET_UPLOAD_MODE` 保持为 `mock`。上传作业状态只保存到 SQLite，不写 JSON 镜像或恢复旧上传任务。服务重启后未完成作业会标记为中断，可在页面重新上传。以上数据不提交到 GitHub。

需要接入平台外的人工采集数据时，点击“上传外部表格”，上传完整步骤明细 Excel，补充来源和数据日期，校验预览后创建发布。每次有效上传建立一个已完成的新批次，自动累计进入汇总和看板；不会覆盖已有发布或清理平台批次草稿。格式、去重、接口和恢复说明见[外部完整表导入](backend/data_publishing/EXTERNAL_IMPORTS.md)。

训练数据总览位于 `/data-publishing/overview`。发布成功后，后台会从冻结完整表自动生成 DataVue 格式的 `all_data.xlsx`，并更新场景／App 看板；无需手动上传汇总表。页面内容、布局、交互及统计口径与提供的 DataVue 原版保持一致，仅配色使用本项目主题。汇总与云道S3上传状态独立，转换状态、警告、重试和完整汇总下载位于发布详情。字段、统计口径、保存位置和接口见[训练数据总览对接说明](backend/training_data_overview/README.md)。

## 7. 生产模式运行

先构建前端，再由 FastAPI 同域托管：

```powershell
npm run build --prefix frontend
python -m uvicorn backend.api:app --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765`。非 API 路由会回退到前端的 `index.html`。

## 8. 停止服务和端口占用

在对应终端按 `Ctrl+C`。如果 PowerShell 提示“终止批处理操作吗 (Y/N)?”，输入
`Y` 并回车。

如果找不到原终端，可查询监听进程：

```powershell
Get-NetTCPConnection -LocalPort 8765,5173 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, State, OwningProcess
```

确认 PID 后再停止对应进程：

```powershell
Stop-Process -Id <PID>
```

出现 `WinError 10013` 或 `address already in use` 时，通常表示端口已被已有服务占用；
如果接口仍可访问，无需重复启动。

如需把后端改到 `9000`，启动前后端时应使用相同代理目标：

```powershell
# 后端终端
python -m uvicorn backend.api:app --reload --host 127.0.0.1 --port 9000

# 前端终端
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:9000"
npm run dev --prefix frontend
```

## 9. 测试

```powershell
python -m unittest discover -s backend\tests -v
npm test --prefix frontend
npm run build --prefix frontend
```

测试不会调用真实模型服务；涉及 Qwen 的路径使用模拟 reviewer。

## 10. 生成动态 Rubric

`backend/DevelopRubrics` 会把本地 rollout 轨迹转换成 AdaRubric 标准对象，并使用
Qwen 生成每一步的截图观察、整条轨迹总结和任务级 Rubric。该模块要求 Python
3.10+，推荐使用已有的 `guigent` Conda 环境：

```powershell
conda activate guigent
python -m pip install -e backend\DevelopRubrics
```

统一入口支持三种命令：

```powershell
# 仅导出标准工作簿，不调用模型
python backend\DevelopRubrics\run_jiawen.py export --skip-model

# 使用 Qwen 导出带观察描述的工作簿
python backend\DevelopRubrics\run_jiawen.py export

# 使用已有工作簿生成 Rubric
python backend\DevelopRubrics\run_jiawen.py generate

# 依次执行模型观察导出和 Rubric 生成
python backend\DevelopRubrics\run_jiawen.py all
```

默认输入为 `backend_workspace/raw/rollout_trajectories`，目录缺失时明确报错，不扫描旧目录。输出为：

```text
backend_workspace/system/
├─ rubric_trajectories.xlsx
├─ rubric_trajectories.json
└─ rubric_outputs/
   └─ rubrics/
      ├─ jiawen_gui_initial_rubric.json
      ├─ jiawen_gui_initial_rubric.evidence.md
      └─ jiawen_gui_initial_rubric.raw_response.txt
```

模型缓存保存到 `backend_workspace/cache/rubric_outputs/`。

工具会递归发现正式轨迹并忽略 `_prefetch_staging`。截图优先使用
`input_stability.jpg`，缺失时依次回退到 `input.jpg` 和 `done.jpg`。模型响应逐次写入
缓存，中断后重新执行同一命令即可续跑。默认关闭 embedding 相似度校验，但仍校验
Rubric JSON、任务 ID、维度数量、权重和 1–5 评分等级。

## 数据与密钥说明

## 11. 批量轨迹质检

### 建树时预生成质检输入

新建的轨迹树任务集会在逐步判断广告、加载和弹窗时，用同一次 Qwen 视觉请求同步生成
post-action observation。所有步骤完成后，系统再为每条轨迹生成一次基于视觉证据的
`final_answer`。有序的 action 与 observation 会在 AdaRubric 评价时自动组成 history，
不需要额外模型调用。

质检输入作为建树快照保存在：

```text
backend_workspace/system/trajectory_tree_runs/<run_id>/rubric_trajectories.json
backend_workspace/system/trajectory_tree_runs/<run_id>/rubric_trajectories.xlsx
```

因此新任务集进入质检时不会再次生成 observation 和 final answer，只需生成缺失的 Rubric
并执行评分。质检必须读取所选任务集随附的 JSON，不回退全局工作簿、旧任务集或旧目录。
任一 observation、final answer 或工作簿生成失败时，本次建树任务集不会发布；已完成缓存会保留供重新提交续跑。

轨迹质检依赖 Python 3.10 以上版本运行 AdaRubric。后端本身仍可使用原有 Python 环境，
并通过 `backend/.env` 中的下列配置启动专用子进程：

```dotenv
ADARUBRIC_PYTHON=D:\anaconda3\envs\guigent\python.exe
```

使用流程：

1. 在“轨迹采集”页完成建树，形成一个时间串任务集。
2. 进入“轨迹质检”，选择该任务集；页面会列出其中全部任务。
3. 勾选一个或多个任务，点击“提交轨迹质检”。后端会全局串行执行模型作业，并显示当前任务、轨迹和完成进度。
4. 读取该任务集声明的质检 JSON；文件缺失、损坏或缺少所需 observation/final answer 时明确报错，不从 Excel 补读或重新生成视觉结果。缺少 Rubric 时按质检流程生成，已完成的本批次模型响应和逐轨迹 checkpoint 可复用。
5. 作业成功后点击“查看轨迹树”。已质检的终点叶子会显示 0–5 分：绿色表示通过，红色表示未通过；点击叶子可查看各维度得分、理由和逐步评价。

最新成功结果保存到：

```text
backend_workspace/system/trajectory_quality_results/<建树任务集 ID>/
```

质检当前状态只保存在 SQLite，不写作业 JSON 镜像；完整 JSON/Excel 当前过程件位于 `backend_workspace/batches/`。服务重启后，未完成作业会标记为
`interrupted`；重新提交相同任务即可从缓存和 checkpoint 续跑。批量作业只有在本次所选任务全部成功后才发布，
失败不会覆盖已有成功结果；重新质检部分任务时也只更新这些任务。

代码仓只保存源码、测试、文档和配置示例。以下内容始终保留在本机：

- `backend/.env` 和其他真实环境变量文件。
- `backend_workspace/` 下的原始轨迹、Excel、任务状态和轨迹树。
- Qwen 分类、对齐和 bbox 复核缓存。
- 所有日志、Python/Node 缓存、依赖和前端构建产物。

## 数据目录迁移与设计文档

当前默认数据根为项目根的 `backend_workspace/`，其内部保留 `system/raw/inputs/resources/batches/releases/cache/logs/tmp` 的统一结构。`ADF_DATA_ROOT` 和现有 API 地址保持原语义。请勿把旧模块目录合并到新目录。

目录切换、回退与路径校验见 [DATA_STORAGE.md](DATA_STORAGE.md)。完整业务流程图和存储设计见 [Word 设计文档](outputs/数据流转与存储设计.docx)。
