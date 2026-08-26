# 2026-08-19 日报

今天主要围绕 Jiawen GUI 轨迹评估流程继续排查和改造，重点关注长轨迹评分时的稳定性、模型输出污染以及实验组织方式。首先发现 evaluator 在 no-thinking 条件下仍然可能把大段显式推理写进 `rationale` 字段，导致结构化 JSON 输出过长甚至被截断。这个问题说明即使关闭模型 thinking，也不能完全避免模型在普通答案字段中自我辩论，因此对 evaluator prompt 和输出 schema 做了收紧，要求 rationale 保持短句，并禁止把内部推理、替代假设和不确定性分析写进 JSON 字段。

随后围绕 action/action_input 与 thinking 的影响设计了三组消融实验：只看 observation 且关闭 thinking、看 action/action_input 且关闭 thinking、只看 observation 且打开 thinking。对应修改了实验 runner 和实验计划文件，使一次运行可以自动按顺序执行这些待办实验，并把每组实验结果分别保存到独立目录中，方便后续比较 action 信息和 thinking 对评分准确性、稳定性、报错率的影响。

在长轨迹分段评分方案上，今天进一步发现 overlap 会带来边界污染。例如评估 Step 5 时，如果给了 Step 6 作为 overlap，模型可能把 Step 6 的终止信息揉进 Step 5 的评分理由里。基于这个观察，先尝试了“看完整轨迹但只输出当前 chunk step”的方式，但进一步分析后认为全局轨迹也可能带来过多无关上下文和历史印象污染。最终改成更细粒度的 step-wise 评估：评价 Step i 时，输入任务、Rubric、历史必要上下文、当前目标 Step i，以及 1 个 look-ahead Step；其中只有 Step i 被评分，历史和未来 step 都明确标注为 context only。

这个新方案的好处是把输出长度控制到每次只评一个 step，同时保留必要的状态转移证据，尤其适合 GUI 任务里需要通过下一步 observation 判断当前操作效果的场景。它也比 overlap 更清晰，因为 prompt 中会显式区分 `TARGET - SCORE THIS STEP` 和 `CONTEXT ONLY - DO NOT SCORE`，减少模型把相邻步骤混在一起评分的风险。今天也同步清理了旧的 overlap 和 chunk_size 配置，改为使用 `evaluation_step_lookahead` 来表达当前策略。

今天的一个重要启发是：当前问题不只是模型能力问题，也和评估输入组织方式强相关。给模型看的上下文太少会导致它无法判断状态变化，给得太多又容易引入混淆和输出膨胀。因此后续实验需要把“输入证据范围”作为一个关键变量来观察，而不是只比较不同模型或不同 Rubric。
