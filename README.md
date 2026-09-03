# Agentic Data Flywheel

一个前后端分离的 GUI Agent 轨迹数据飞轮工程，包含场景任务生成、失败任务扩增、轨迹 Excel
导出、动作 bbox 标注、受限中间态过滤、任务级轨迹树构建，以及 Vue 轨迹采集与质检界面。

## 主要能力

- 递归读取 rollout 轨迹并导出 Excel。
- 为 `click`、`swipe`、`long_press` 动作生成并复核 bbox。
- 使用 Qwen 判断广告、加载、弹窗等临时中间状态。
- 在仅忽略短暂插入状态的前提下构建 action 前缀树。
- 在网页中按真实场景能力树生成、审核和导出任务。
- 上传失败任务并自动匹配场景、扩增变体任务。
- 在网页中浏览任务、轨迹、步骤截图和动作标注。
- 在线修改 bbox，并将结果更新到标注 Excel。
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
├─ backend_workspace/            本地数据与运行结果，不提交 Git
│  ├─ rollout_trajectories/      原始轨迹放置目录
│  ├─ task_generation/           任务生成知识库、作业和导出结果
│  │  └─ KnowledgeBase/         三份任务生成 Excel 知识库
│  └─ trajectory_correction/     轨迹修正输入、草稿与导出
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

把任务目录放入：

```text
backend_workspace/rollout_trajectories/
```

预期结构示例：

```text
backend_workspace/rollout_trajectories/
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

在项目根目录运行：

```powershell
python backend\trajectories_preprocessing.py
```

处理流程为：

1. 递归读取正式轨迹并忽略 `_prefetch_staging` 等临时目录。
2. 生成 `backend_workspace/trajectories_to_excel.xlsx`。
3. 为目标动作生成候选 bbox，并调用 Qwen 复核。
4. 生成 `backend_workspace/annotated_trajectories.xlsx`。

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
backend_workspace/task_generation/KnowledgeBase/
├─ VLA场景树.xlsx
├─ APP操控先验知识库.xlsx
└─ APP资源先验知识库.xlsx
```

进入“场景能力树”，按 **场景 → 一级能力 → 任务类型** 浏览。点击节点查看详情，勾选节点选择生成范围；App 不再作为树的根节点。任务类型数量按路径去重，App 数量单独计入执行单元。

- 点击“编辑场景树”可新增、重命名、删除三级节点，并按 App 编辑参考示例、资源先验开关。修改统一保存或取消，离开页面前会提示未保存内容；编辑期间不能提交生成。
- 每个选中任务类型默认勾选全部适用 App，可逐项取消。例如一个任务类型选择三个 App、数量设为 5，预计生成 15 条主任务；弱依赖前置任务另计。
- 空场景、空能力和未配置 App 的任务类型可以保存，后者不可生成。缺少操控或资源先验会显示提示，不阻止其他已就绪配置的生成。
- 生成完成后可编辑任务文本、成组删除/恢复弱依赖任务并导出 Excel。历史作业使用提交时的快照，不受后续编辑影响。

首次使用时，系统从上述三个 Excel 初始化知识库版本。根目录原文件保留，之后 **`KnowledgeBase/current.json` 指向的 `versions/<版本>/` 才是当前有效知识库**；请通过网页替换文件，不要直接修改根目录旧文件或版本目录。
每个版本保存三份 Excel 和 `scene_tree.json`（稳定 UUID 与树结构），通过原子切换版本指针一起发布。改名同步更新操控先验路径；删除节点或移除 App 不删除已有先验记录。旧版本目录完整保留，作为备份。

知识库卡片支持单文件替换和“下载已保存场景树”。下载文件保留六个业务列，并带有隐藏的 `_scene_tree_nodes` sheet，用于保留节点 UUID、空分支及无 App 的任务类型；回传时请保留该 sheet。普通六列表仍支持导入，相同路径复用当前 UUID。不同 App 的示例/资源配置分别保存，同一任务类型/App 出现冲突行时拒绝导入并提示修正。多个标签页同时保存时，旧版本请求返回 `409`，不会覆盖新版本。

场景树接口：`GET /api/task-generation/tree` 返回 `version/scenes/leaf_count/execution_unit_count/warnings`；`PUT` 同路径接收 `base_version/scenes`；`GET /api/task-generation/tree/export` 下载当前 Excel。生成提交采用 `{version, selections: [{node_id, apps}], generate_n}`，不再使用 App 展开的 `node_ids`；历史作业读取接口不变。

如需无模型费用、无业务数据改动的页面验收，先构建前端，再运行 `python -m backend.tests.scene_tree_demo_server --port 8791`，访问 `http://127.0.0.1:8791/task-generation/scenario-tree`。该验收服务把知识库复制到临时目录，并使用模拟模型；按 Ctrl+C 结束后清理临时数据，不应将其作为正式服务运行。

进入“任务扩增”后上传失败任务 Excel。原始表需要包含 `任务`、`涉及APP`，如果存在 `任务结果` 列则只扩增非 `TRUE` 行；也可以直接上传含有 `app/task/scene/capability/sub_capability` 的 `新场景匹配` 表。扩增结果同样可以审核、删除和导出。

任务生成作业和导出结果保存在：

```text
backend_workspace/task_generation/
├─ jobs/       # 作业状态 JSON
├─ runs/       # 知识库快照、输入文件和结果 JSON
├─ exports/    # 导出的任务 Excel
└─ logs/       # 本地日志
```

如需使用不同模型，可在 `backend/.env` 中设置 `TASK_GENERATION_MODEL_NAME`、`TASK_GENERATION_MODEL_URL` 和 `TASK_GENERATION_API_KEY`；未设置时回退到通用 `MODEL_NAME`、`MODEL_URL` 和 `YUNAI_API_KEY`。并发数使用 `TASK_GENERATION_MAX_CONCURRENT`，默认值为 4。

### 轨迹采集

1. 进入“轨迹采集”，查看从工作区发现的任务和轨迹名称。
2. 展开任务并选择一条轨迹，按需查看每一步截图、action、summary 和 bbox。
3. 点击截图右上角的“修改 bbox”，重新绘制并保存当前动作框；系统会更新
   `annotated_trajectories.xlsx`。
4. 勾选一个或多个已预处理任务并提交建树，等待后台作业完成。

### 轨迹质检

1. 选择一次成功建树形成的时间串任务集。
2. 选择具体任务，查看以桌面为统一起点的 action 前缀树。
3. 点击节点查看 action、summary、bbox、分类信息和所有 occurrence 截图。
4. 在中间态审计中查看未计入树的短暂广告、加载或弹窗步骤。

建树结果保存在：

```text
backend_workspace/trajectory_tree_runs/<完成时间串>/
```

作业状态保存在 `backend_workspace/trajectory_tree_jobs/`。这些都是本地运行产物，
不会提交到 GitHub。

### 轨迹修正

进入“轨迹纠偏 → 专家动作纠偏”，选择已质检批次。当前每个任务仍按原规则选取质检 Top-1；同分时保留原工作簿顺序。已有批次自动恢复原草稿和入选轨迹，不用新推荐覆盖；源标注表版本不匹配时仍拒绝修正。

页面按“任务行（用例编号）→ 轨迹行 → 修正台”展开，首次进入和刷新默认全部收起。可同时展开多个任务，但整页只展开一个轨迹修正台，展开轨迹才加载步骤与截图。任务统计与轨迹统计分开；前端已预留一任务多轨迹的结构，本次没有开放 Top-3 筛选。

1. 展开轨迹后，左侧查看截图并直接修正 Action，右侧选择步骤；click/long_press 点击图片取点，swipe 按住拖动取起止点，坐标以图片角标显示。
2. Action 点击“保存动作”保存。收起、切换步骤/轨迹/批次、离开页面或导出前保护未保存输入：动作可选择保存、放弃或取消；保存失败保留当前位置和输入。
3. 删除/恢复步骤、加入/取消导出即时保存到草稿。收起轨迹不影响导出开关；“已修改”不代表审核完成。
4. 导出生成 SFT、RL、原生完美通过、原生异常待处理四类工作表；原 Excel 不会被覆盖。批次详情和导出历史默认折叠。

无真实数据和模型调用的浏览器验收：先执行 `npm run build --prefix frontend`，再执行 `python -m backend.tests.correction_demo_server --port 8792`，访问 `http://127.0.0.1:8792/correction/expert-action`。模拟服务提供一个三轨迹任务及一个单轨迹批次，编辑仅驻留内存，重启即丢弃；模拟导出仅用于下载交互检查，实际四类数据分流由后端单元测试覆盖。该服务不是正式后端。

该模块的数据目录为：

```text
backend_workspace/trajectory_correction/
├─ inputs/       # 历史输入目录；当前批次来源为正式 annotated_trajectories.xlsx
├─ sessions/     # 草稿 JSON
└─ exports/      # 导出 Excel
```

### 数据发布

进入“专家工作台 → 数据发布”后，可以把 1～N 个尚未发布的纠偏会话登记为一个数据集。发布前，每个会话必须已经通过 COT 工作台右上角的“导出数据集”生成至少一份 `full_dataset` Excel；系统始终选取该会话最新的一份完整导出，不重新生成或合并 Excel。

创建时填写可读的数据集名称，系统生成唯一的 `rel_<随机哈希>` 发布 ID。发布记录保存在：

```text
backend_workspace/dataset_release/releases.json
```

记录包含纠偏 Excel 的项目相对路径、SHA256、行数，以及整个 `backend_workspace/rollout_trajectories` 根目录的位置；不会保存显式的纠偏会话 ID，也不会复制、收集或去重轨迹。发布成功的会话会从专家纠偏界面隐藏，但会话草稿和导出文件仍保留在本地。

页面下半部分展示全部历史数据集，支持按名称或发布 ID 搜索、按上传状态筛选、查看路径与哈希、下载发布 Excel。点击“上传训练环境”会启动独立的模拟上传作业：后端遍历发布 Excel 和完整轨迹根目录、统计文件与字节并持续报告进度，但不会向外部网络发送文件。成功后会生成类似下面的模拟地址：

```text
s3://training-data/gui-agent-datasets/rel_a84f91c25d3e4b67/
```

目标地址可通过 `backend/.env` 中的 `DATASET_S3_BUCKET` 和 `DATASET_S3_PREFIX` 调整；当前 `DATASET_UPLOAD_MODE` 必须保持为 `mock`。上传作业保存在 `backend_workspace/dataset_release/upload_jobs/`，服务重启后未完成作业会标记为中断，可在页面重新上传。以上发布记录、作业和数据文件均为本地运行产物，不提交到 GitHub。

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

默认输入为 `backend_workspace/rollout_trajectories`，输出为：

```text
backend_workspace/
├─ rubric_trajectories.xlsx
└─ rubric_outputs/
   ├─ cache/qwen_summaries.json
   └─ rubrics/
      ├─ jiawen_gui_initial_rubric.json
      ├─ jiawen_gui_initial_rubric.evidence.md
      └─ jiawen_gui_initial_rubric.raw_response.txt
```

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
backend_workspace/trajectory_tree_runs/<run_id>/rubric_trajectories.xlsx
```

因此新任务集进入质检时不会再次生成 observation 和 final answer，只需生成缺失的 Rubric
并执行评分。旧任务集若没有随附工作簿，仍会回退到全局工作簿及原有自动补齐流程。
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
4. 若 observation、final answer 或 Rubric 缺失，作业会自动生成；已完成的模型响应和逐轨迹 checkpoint 会被复用。
5. 作业成功后点击“查看轨迹树”。已质检的终点叶子会显示 0–5 分：绿色表示通过，红色表示未通过；点击叶子可查看各维度得分、理由和逐步评价。

最新成功结果保存到：

```text
backend_workspace/trajectory_quality_results/<建树任务集 ID>/
```

质检作业状态保存在 `backend_workspace/trajectory_quality_jobs/`。服务重启后，未完成作业会标记为
`interrupted`；重新提交相同任务即可从缓存和 checkpoint 续跑。批量作业只有在本次所选任务全部成功后才发布，
失败不会覆盖已有成功结果；重新质检部分任务时也只更新这些任务。

代码仓只保存源码、测试、文档和配置示例。以下内容始终保留在本机：

- `backend/.env` 和其他真实环境变量文件。
- `backend_workspace` 下的原始轨迹、Excel、任务状态和轨迹树。
- Qwen 分类、对齐和 bbox 复核缓存。
- 所有日志、Python/Node 缓存、依赖和前端构建产物。
