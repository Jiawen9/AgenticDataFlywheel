from __future__ import annotations

import json


def system_prompt(scene: str, capability: str, sub_capability: str, description: str, app: str, resource_prior: object, reference_example: object, generate_n: int) -> str:
    return f"""
# Role
你是一名资深的移动端用户行为专家，负责为 GUI Agent 生成真实、口吻化的用户操作任务（Task）。

# Context
- 【业务场景】: {scene}
- 【一级能力】: {capability}
- 【二级能力】: {sub_capability}
- 【二级能力参考】: {description}
- 【目标 App】: {app}
- 【App 先验信息 (可选引用源)】:
{resource_prior}
- 【参考示例】: "{reference_example}"

# Task
请结合上述维度，为该 App 生成 {generate_n} 条真实、具有发散性且逻辑严密的 GUI 操作指令。

# Requirements & Constraints
1. 若涉及具体内容选择，必须且仅能从【App 先验信息】中选取实体；通用功能操作可根据目标 App 的实际功能发挥。
2. 使用自然口吻，严禁机械复读二级能力描述中的术语。
3. 任务必须是进入 App 首页后能独立完成的完整链路。
4. 指令中必须自然包含 App 名称“{app}”。
5. 任务意图符合二级能力，但不要机械改写参考示例。
6. JSON 中的 app、scene、capability、sub_capability 必须与 Context 完全一致。

# Output Format
请直接输出 JSONL，每行一个合法 JSON 对象，不要输出 Markdown、编号或其他解释：
{{"app":"{app}","scene":"{scene}","capability":"{capability}","sub_capability":"{sub_capability}","task":"生成的任务描述"}}
""".strip()


def scene_classification_prompt(scene_tree_text: str, task: str, app: str) -> str:
    return f"""# Role
你是一个高精度的移动端自动化数据标注专家，负责将用户任务准确归类到预定义场景树。

# Scene Mapping Table
{scene_tree_text}

# Target Task
- 任务描述：{task}
- 当前应用：{app}

# Rules
1. 只能在“涵盖App”包含 {app} 的行中寻找匹配项。
2. 输出的三个层级必须一字不差地来源于场景映射表，不能自行捏造或同义替换。
3. 根据任务意图匹配；无法匹配时三个层级均输出 Unclassified。

# Output
只输出严格 JSON 对象：
{{"scene":"场景或Unclassified","capability":"能力或Unclassified","sub_capability":"子能力或Unclassified","reason":"简短理由"}}""".strip()


def dependency_prompt(task: str, app: str) -> str:
    return f"""# Role
你是一个高级 AI 数据标注专家，负责为 GUI Agent 判断任务是否依赖 App 中的历史状态。

# Target Task
- 任务描述：{task}
- 当前应用：{app}

# Rules
1. 默认 App 已正常登录，不要生成登录或注册前置任务。
2. 如果从 App 首页即可完成，输出 zero；导航、查找和搜索属于执行步骤，不属于前置依赖。
3. 如果必须依赖过去产生的历史数据且不需要外部设备，输出 weak，并逆向生成一条单一、客观、无 UI 导航路径且包含 App 名称的前置任务。
4. 如果依赖短信验证码、扫码、真实支付卡、人脸识别等外部物理条件，输出 strong。

# Output
只输出严格 JSON 对象：
{{"dependency_relationships":"zero/weak/strong","pre_task":"weak 时填写前置任务，否则为 null","reason":"简短理由"}}""".strip()


def flywheel_prompt(failed_case: dict[str, object], resource_prior: object, generate_n: int) -> str:
    app = str(failed_case.get("app", "未知App"))
    scene = str(failed_case.get("scene", "未知场景"))
    capability = str(failed_case.get("capability", "未知能力"))
    sub_capability = str(failed_case.get("sub_capability", "未知子能力"))
    failed_task = str(failed_case.get("task", "未知任务"))
    description = str(failed_case.get("sub_capability_desc", "请参考二级能力字面意思执行。"))
    reference = str(failed_case.get("reference_example", "暂无参考示例"))
    return f"""# Role
你是一名资深的移动端自动化测试架构师与数据构造专家，负责通过构造对抗性的变体任务修复 Agent 的能力缺陷。

# Context
- 【目标 App】: {app}
- 【业务场景】: {scene}
- 【一级能力】: {capability}
- 【二级能力】: {sub_capability}
- 【二级能力参考】: {description}
- 【失败的原始任务】: {json.dumps(failed_task, ensure_ascii=False)}
- 【App 先验信息】: {resource_prior}
- 【参考示例】: {json.dumps(reference, ensure_ascii=False)}

# Task
请生成 {generate_n} 条符合上述维度、口语化且有发散性的训练变体任务。

# Constraints
1. 保留 App 名称和操作意图骨架，但替换原任务中的具体内容实体；新实体优先从 App 先验信息中选取。
2. 改变句式和动词表达，不要机械复读原任务。
3. 任务起点默认为进入 App 首页，必须自然包含“{app}”。
4. 任务只能属于当前二级能力，不得发散到其他页面功能。

# Output
直接输出 JSON 数组，不要输出 Markdown 或其他解释：
[{{"task":"变体任务描述"}}]""".strip()
