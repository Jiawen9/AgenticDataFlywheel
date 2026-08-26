# Rubric Generation Zero-Shot Chat Prompt

这个文件用于测试聊天框大模型在没有 few-shot example 的情况下，为 `supply_chain_eval.py` 采购任务生成 AdaRubric `DynamicRubric` 的能力。

使用方式：把下面整个 prompt 一次性复制到聊天框中。不要分两次发送。

```text
You are an expert evaluation rubric designer for AI agent trajectories.

Your task: given a description of an agentic task, produce a dynamic evaluation rubric consisting of 5 orthogonal dimensions, each with a 5-point scoring scale.

### Design Constraints

1. Task-Specific: Every dimension must be directly derived from the task requirements.
   Do NOT use generic dimensions like "helpfulness" or "coherence" unless the task specifically requires them.
2. Observable: Each dimension must be assessable from the agent trajectory
   (Thought -> Action -> Observation steps). Do not create dimensions that require information outside the trajectory.
3. Orthogonal: Minimize correlation between dimensions. Each dimension should capture a distinct aspect of performance.
4. Calibrated: The 5-point scale must have concrete, distinguishable criteria at every level.
   Score 3 = acceptable baseline. Score 1 = fundamentally broken. Score 5 = exemplary execution.
5. Weighted: Assign weights from 0.5 to 2.0 reflecting each dimension's relative importance to overall task success.

### Output Format

Return a JSON object with these fields:
- task_id: string
- dimensions: array of exactly 5 dimension objects, each containing:
  - name: string, concise PascalCase
  - description: string, what this dimension measures
  - weight: number, 0.5 to 2.0
  - scoring_criteria: object mapping scores 1 to 5 to concrete behavioral descriptions
- generation_rationale: string explaining why these dimensions were chosen

### Task Information

- Task ID: sc-001
- Instruction: You are a procurement agent. Find 3 suppliers of industrial-grade ball bearings (6205-2RS type) in the EU, request quotes for 10,000 units, compare total cost including shipping, and recommend the best value option considering both price and lead time.
- Domain: Procurement API Orchestration
- Complexity: complex
- Expected Tools: supplier_search, request_quote, shipping_calculator, compare_options, submit_recommendation
- Additional Context: {"budget_eur": 50000, "delivery_deadline_days": 30, "quality_standard": "ISO 9001"}

Generate the evaluation rubric now.

You MUST respond with valid JSON only. Do not include markdown, code fences, comments, or explanation outside JSON.

The JSON must match this structure exactly:

{
  "task_id": "sc-001",
  "dimensions": [
    {
      "name": "PascalCaseDimensionName",
      "description": "What this dimension measures.",
      "weight": 1.0,
      "scoring_criteria": {
        "1": "Concrete criteria for fundamentally broken performance.",
        "2": "Concrete criteria for weak performance.",
        "3": "Concrete criteria for acceptable baseline performance.",
        "4": "Concrete criteria for strong performance.",
        "5": "Concrete criteria for exemplary performance."
      }
    }
  ],
  "generation_rationale": "Why these dimensions were chosen."
}
```

## 输出检查

拿到聊天模型输出后，先检查：

- 是否是纯 JSON，没有 markdown 代码块。
- `task_id` 是否为 `sc-001`。
- `dimensions` 是否正好有 5 个。
- 每个维度是否都有 `name`、`description`、`weight`、`scoring_criteria`。
- `scoring_criteria` 是否包含 1 到 5 五个等级。
- 维度是否围绕采购任务本身，而不是泛泛的 helpfulness / clarity。
- 维度之间是否尽量不重叠。
