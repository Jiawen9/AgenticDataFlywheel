# 专利 IDEA：基于任务感知 Rubric 与差分 Observation 的 GUI 智能体轨迹逐步评价方法

## 名称

一种基于任务感知 Rubric 与差分 Observation 的 GUI 智能体轨迹逐步评价方法。

## 核心 Idea

本方案面向 GUI 智能体轨迹评价中存在的任务标准不一致、长轨迹评分不稳定、单步操作效果难以从 action 判断等问题，提出一种基于任务感知 Rubric、差分 Observation 和上下文隔离式逐步评分的自动评价方法。

在评价前，系统根据任务描述和稳定环境证据生成一套任务感知 Rubric。该 Rubric 在同一任务的一组候选轨迹评价过程中保持固定，用于保证不同轨迹之间评分标准一致。

在轨迹表示层，系统不只记录单步 action，而是构建差分 Observation，即描述当前步骤执行前后 GUI 状态的变化，包括页面跳转、控件状态变化、目标对象是否出现、按钮是否从未选中变为已选中等。这使评价模型能够基于可观察状态变化判断任务是否推进，而不是依赖 action 坐标或模型臆测。

在评分阶段，系统采用逐 step 评价。评价 Step i 时，输入包括任务、固定 Rubric、历史上下文、当前目标 Step i，以及有限 look-ahead Step。其中历史步骤和 look-ahead 步骤只作为 context，当前 Step i 被标记为 target，模型只输出 Step i 的评分结果。最后，系统对每个 step-dimension 的分数进行改进后的 confidence-normalized 聚合，得到维度分和全局分。

## 创新点

### 1. 差分 Observation 驱动的 GUI 轨迹评价证据构建方式

传统轨迹评价通常依赖 action、action_input 或单点 observation，但 GUI 任务中真正可验证的证据往往是操作前后的状态变化。本方案将 observation 构建为前后状态差分描述，用于表达页面是否跳转、目标控件是否出现、按钮状态是否变化、任务目标是否完成等可观察证据。该差分 Observation 可同时用于 Rubric 构建和 step 评分。

### 2. 基于差分 Observation 的任务感知 Rubric 生成方式

Rubric 不仅根据任务文本生成，还结合稳定环境证据和差分 Observation 中体现的可观察状态变化生成评价维度与评分标准。这样生成的 Rubric 更贴合 GUI 任务的真实完成条件，而不是只停留在抽象任务描述层面。生成后的 Rubric 在同一任务候选轨迹评价中固定使用，避免评价标准随轨迹变化。

### 3. 上下文隔离式逐 step 评价机制

系统将当前被评价步骤显式标记为 target step，将历史步骤和未来 look-ahead 步骤标记为 context only。模型只能输出 target step 的评分，不能输出 context step 的评分，从而减少长轨迹评价中的步骤混淆和 chunk 边界污染。

### 4. 有限 look-ahead 的状态验证机制

对于 GUI 操作，当前 step 的效果往往需要通过下一步 observation 才能确认。因此评价 Step i 时，系统引入有限 look-ahead Step，尤其是 Step i+1 的差分 Observation，用于辅助判断 Step i 是否造成了正确状态变化。但 look-ahead step 只作为上下文证据，不被评分。

### 5. 对 AdaRubric confidence 聚合方式的改进

AdaRubric 原方法中已有 step-dimension confidence，但直接使用原聚合方式可能导致无关步骤稀释维度得分，或者使好轨迹与差轨迹区分度不足。本方案将 confidence 明确作为证据适用性权重，并采用 confidence-normalized 聚合，使某一维度的得分主要由对该维度提供有效证据的步骤决定，从而提升轨迹好坏的区分能力。

### 6. 面向 LLM 结构化评分输出的容错机制

针对模型可能出现的长 rationale、字段错位、summary 被误放入 dimension_scores、输出非目标 step 等问题，系统提供结构化解析与修复逻辑，保证长流程自动评价不会因单个格式错误中断。

## 可取证点

### 1. 轨迹数据中的 Observation 字段

如果系统中的 observation 不是单纯静态页面描述，而是包含“操作前状态 -> 操作后状态”的变化信息，例如页面跳转、控件状态变化、按钮选中状态变化、目标内容是否出现等，则可证明其采用差分 Observation 作为评价证据。

### 2. Rubric 生成输入

如果 Rubric 生成阶段除任务描述外，还输入了稳定环境证据或差分 Observation 摘要，并且生成的 Rubric 维度中体现了可观察状态变化要求，例如“目标页面到达”“目标控件状态变化”“最终完成状态证据”等，则可证明 Rubric 构建使用了 GUI 可观察证据。

### 3. Rubric 文件

Rubric 文件中应包含任务专属的 dimension name、description、weight 和 1-5 scoring criteria。同一任务下多条候选轨迹共用同一 Rubric 文件，可证明 Rubric 是任务感知且评价阶段固定使用。

### 4. 评价请求 prompt

评价 Step i 时，请求中会出现 target/context 标记，例如：

```text
TARGET - SCORE THIS STEP
CONTEXT ONLY - DO NOT SCORE
```

并且输入中包含历史上下文、当前 Step i 和有限 look-ahead Step。这可证明系统采用上下文隔离式逐步评价。

### 5. Look-ahead 使用方式

评价 Step i 的输入中包含 Step i+1 的 Observation，但输出结果只包含 Step i 的评分，不包含 Step i+1 的评分。这可证明 look-ahead 仅作为状态验证上下文，而不是被评价对象。

### 6. 评分结果 JSONL

结果文件中应保存每个 step 在每个 Rubric 维度下的 score、confidence、rationale，以及最终 dimension_global_scores 和 global_score。若某一维度的聚合分是按该维度相关步骤的 confidence 归一化计算得到，则可证明采用了改进后的 confidence-normalized 聚合。

### 7. 异常处理日志

如果模型输出中出现超长 rationale、字段错位或 summary 被放入 dimension_scores，系统仍能恢复结构化结果并继续评价，可证明其具备 LLM 评分输出容错机制。

## 一句话总结

本方案提出一种基于任务感知 Rubric 与差分 Observation 的 GUI 智能体轨迹逐步评价方法，通过前后状态变化证据、target/context 隔离、有限 look-ahead 验证以及对 AdaRubric confidence 聚合方式的修正，实现对 GUI 长轨迹更稳定、可解释、可区分的自动评价。
