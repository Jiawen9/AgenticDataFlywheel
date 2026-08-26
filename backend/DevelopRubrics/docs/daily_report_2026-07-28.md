# Daily Report - 2026-07-28

## 今日工作总结

今天主要围绕 AdaRubric 项目完成了代码理解、运行调试、本地模型配置适配、方法论问题梳理和文档沉淀。

## 1. 项目整体理解

- 梳理了 AdaRubric 的核心目标：根据 `TaskDescription` 动态生成任务专属 `DynamicRubric`，再用 rubric 对 agent 的 `Trajectory` 进行逐步评估。
- 明确了项目主流程：

```text
TaskDescription
  -> DynamicRubric
  -> TrajectoryEvaluation
  -> Aggregation / Filtering / Reward Data
```

- 阅读并理解了核心模块：
  - `adarubric/pipeline.py`
  - `adarubric/core/models.py`
  - `adarubric/generator/llm_generator.py`
  - `adarubric/evaluator/trajectory_evaluator.py`
  - `adarubric/llm/openai_client.py`

## 2. Quickstart 运行与环境调试

- 创建了项目本地虚拟环境 `.venv`。
- 安装了项目依赖和 dev 依赖。
- 运行 `examples/quickstart.py`，定位到 API key 和 API quota 问题。
- 明确了 ChatGPT Plus 和 OpenAI API billing 是两套独立体系。
- 验证了项目测试集：

```text
94 passed
```

## 3. 本地模型服务适配

- 修改了 `examples/supply_chain_eval.py`，使其支持通过环境变量连接 OpenAI-compatible 本地模型服务。
- 新增支持的环境变量：

```powershell
$env:ADARUBRIC_BASE_URL
$env:ADARUBRIC_API_KEY
$env:ADARUBRIC_MODEL
```

- 这样可以在不硬编码 key 和模型地址的情况下运行 supply chain 示例。

## 4. 核心概念学习与代码走读

- 重点理解了 `TaskDescription` 的作用和字段含义。
- 重点理解了 `Trajectory` / `TrajectoryStep` 的结构。
- 分析了 `_validate_step_ordering` 的作用：保证 step id 唯一且单调递增，避免后续 step-level evaluation 错位。
- 梳理了 `generate_rubric` 的参数和执行逻辑。
- 明确了模型推理实际发生的位置：

```text
adarubric/llm/openai_client.py
  -> self._client.chat.completions.create(...)
```

## 5. Rubric 与 Evaluation 文档沉淀

- 新增 `docs/dynamic_rubric_template.md`：
  - 整理了 `DynamicRubric` 的结构。
  - 提供了可填写的 rubric 设计模板。
  - 包含 dimension、description、weight、scoring criteria 等字段。

- 新增并迭代 `docs/trajectory_evaluation_template.md`：
  - 整理了如何用 `DynamicRubric` 对 `Trajectory` 打分。
  - 区分了 evaluation workflow 和 output structure。
  - 补充了 evaluation system prompt / user prompt 的重要性。

- 新增并迭代 `docs/adarubric_method_risks.md`：
  - 梳理了当前 AdaRubric 方法中暴露出的关键问题。
  - 重点包括：
    - 缺少 rubric 质量评估。
    - 需要 calibration trajectories 校准 rubric。
    - 固定 5 个维度不一定适合所有任务域。
    - system prompt 对方法行为有重要影响，需要通过验证集优化。

## 6. GitHub 推送

今天完成了多次 GitHub 推送，仓库地址：

```text
https://github.com/youzi-baby/DevelopRubrics
```

主要推送内容包括：

- 初始 AdaRubric 项目代码。
- `some_orders.py`。
- `docs/dynamic_rubric_template.md`。
- `docs/trajectory_evaluation_template.md`。
- `docs/adarubric_method_risks.md`。

## 明日可继续推进

- 设计 `RubricEvaluator` / `RubricSelector`，用于评估和筛选候选 rubrics。
- 设计 calibration trajectories，用于测试 rubric 是否能区分 good / medium / weak trajectories。
- 做不同 `num_dimensions` 下的敏感性分析。
- 为 generator/evaluator system prompt 建立版本管理和验证集评估流程。
