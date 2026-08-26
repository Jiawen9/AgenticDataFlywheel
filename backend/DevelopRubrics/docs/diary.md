# 今日工作总结

今天主要围绕 Jiawen GUI 轨迹评估实验继续推进，重点处理了 judge 模型能力、长轨迹评估稳定性、thinking 输出污染，以及后续模型切换配置的问题。整体上，我今天的工作不是单纯跑脚本，而是在把当前 AdaRubric 评估流程改造成一个更适合真实 GUI 任务验证的实验框架。

首先，我继续分析了当前 rubric-based evaluator 在 Jiawen 数据上的表现。前面的实验说明，模型通常能够区分轨迹之间的相对好坏，但在绝对 pass/fail 判断上仍然不够稳定，尤其容易出现失败轨迹被判为 pass 的情况。这让我进一步意识到，单纯依靠多个 rubric 维度加权平均，可能会把“任务是否真正完成”这个关键问题稀释掉。因此今天明确了一个后续优化方向：把 Completion 从普通 rubric 维度里拆出来，做成第一层 Hard Gate，先判断任务目标是否真的完成，再在完成或未完成的组内用 rubric score 进行质量排序。

在长轨迹评估方面，我进一步确认了 full trajectory 一次性输入虽然语义最完整，但在本地模型服务上容易触发 502 或请求超时；而简单按固定 step 切 chunk 又会带来边界上下文断裂的问题。为了解决这个问题，今天采用了带 overlap 的 chunk 方案：以 5 个 step 为一个核心 chunk，相邻 chunk 之间保留 1 个 step 的上下文重叠。重叠部分只作为 judge 判断时的上下文参考，最终聚合时只保留每个 chunk 的核心 step 分数，避免重复计分。这个方案在可运行性和上下文完整性之间做了折中。

同时，我也补充了一个 Direct Judge baseline，用来和 AdaRubric evaluator 做对比。这个 baseline 不给模型看 rubrics、不看人工排序、不看 GT，也不看 thought 和 final_answer，只给 Task 以及每条轨迹中可观察的 Action、Action Input 和 Observation，让模型直接对候选轨迹从优到劣排序并给出原因。这个实验可以帮助判断：当前问题到底来自 rubric 设计、step-level 打分机制，还是模型本身的 judge 能力。如果 Direct Judge 的排序能力明显更强，就说明 AdaRubric 的结构化评分机制还需要继续改造。

今天还处理了 Qwen3.6-27B 这类 thinking 模型的使用问题。由于 judge 任务本身比较难，完全关闭 thinking 可能会损失模型判断能力；但如果模型把 `<think>...</think>` 思考内容直接混进最终输出，又会污染 JSON 解析和实验结果。因此今天在底层 JSON 抽取逻辑中加入了 thinking 清理机制：模型可以输出 thinking，但在解析结构化 JSON 和保存 raw response 前，会自动剥离 `<think>...</think>` 内容。这样既允许模型进行推理，又保证最终进入评估结果的只有结构化答案。

为了避免频繁在 PowerShell 里切换环境变量，今天还把 thinking 相关的 `extra_body` 配置放进了 `examples/jiawen_rubric_config.json`。现在 Qwen 模型需要打开 thinking 时，可以直接在 config 里配置 `chat_template_kwargs.enable_thinking=true`；后面如果切回 dsv4，只需要把 config 里的 `extra_body` 改成 `null`，不需要再反复设置或清理系统环境变量。这个改动也统一作用于 rubric generation、rubric evaluation 和 direct judge baseline 三个脚本。

今天的一个重要启发是：真实 GUI 任务评估里，模型 judge 的能力、输入上下文长度、是否暴露 thought、是否使用 rubrics、是否强制结构化输出，都会显著影响实验结果。因此后续不能只看某一次 global score，而应该系统比较几类方案：rubric-based evaluator、completion hard gate、direct judge baseline，以及不同模型在相同输入约束下的排序和 pass/fail 表现。这样才能判断 AdaRubric 在当前任务上到底提供了多少额外价值，以及我们需要重点优化哪一层。
