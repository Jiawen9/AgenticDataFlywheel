

def system_prompt(scene, capability, sub_capability, sub_capability_desc, target_app, app_prior, reference_example, generate_n):
    return f"""
    # Role
    你是一名资深的移动端用户行为专家，负责为 GUI Agent 生成真实、口吻化的用户操作任务（Task）。

    # Context
    - 【业务场景】: {scene}
    - 【一级能力】: {capability} 
    - 【二级能力】: {sub_capability}
    - 【二级能力参考】: {sub_capability_desc}
    - 【目标 App】: {target_app}
    - 【App 先验信息 (可选引用源)】:
    {app_prior}
    - 【参考示例】: "{reference_example}"

    # Task
    请结合上述维度，为该 App 生成 {generate_n} 条真实、具有发散性且逻辑严密的 GUI 操作指令。

    # Requirements & Constraints
    1. **条件化引用先验实体**: 
       - **若涉及具体内容选择**：必须且仅能从【App 先验信息】中选取实体。严禁捏造名单外的地名、影视剧或人名。
       - **若涉及通用功能操作**：请根据【目标 App】的实际功能逻辑自由发挥，确保任务真实可行。
    2. **口吻自然化 (User-like Tone)**: 
       - 严禁机械复读“二级能力描述”中的术语。
       - 使用口头语、缩略语或带有主观诉求的语气（例如：“帮我把...给弄下”）。
    3. **隐含首页起点 (Implicit Home Start)**: 生成的任务必须是用户在【进入 App 首页后】能独立完成的完整链路。
    4. **显式包含 App 名称**: 指令中必须自然地包含“{target_app}”。
    5. **逻辑对齐而非字面对齐**: 指令只需在“意图”上符合【二级能力参考】即可，严禁机械改写参考示例。
    6. **数据一致性**: JSON 中的 `scene`, `capability`, `sub_capability`, `app` 字段必须与 Context 中的输入完全一致。

    # Output Format
    请直接输出 JSONL 格式（每行一个合法的 JSON 对象），不要包含 Markdown 代码块标记（如 ```json）、编号或其他多余解释。
    每条 Task 的输出格式严格如下：
    {{ "app": "{target_app}", "scene": "{scene}", "capability": "{capability}", "sub_capability": "{sub_capability}", "task": "生成的任务描述" }}
    """


def get_sce_by_task(scene_tree_text, task_description, current_app):
    return f"""# Role
你是一个高精度的移动端自动化数据标注专家，负责将用户执行的任务（Task）准确归类到预定义的场景树中。

# Context
我有一份标准场景映射表。每一行定义了一个完整的三级场景路径，以及该场景所涵盖的 App 范围。你需要根据输入的 Target Task 信息，找到唯一且完全匹配的场景路径。特别注意，当前任务可能是一个“前置准备任务”（如：登录、搜索、打开特定页面等）。

# Input Data
## 1. 场景映射表 (Scene Mapping Table)
{scene_tree_text}

## 2. 待处理任务 (Target Task)
- 任务描述：{task_description} 
- 当前应用：{current_app}

# Reasoning Logic
1. **App 绝对筛选**：必须且只能在“涵盖App”包含 {current_app} 的行中寻找匹配项。
2. **防幻觉红线**：输出的 scene、capability、sub_capability 必须一字不差地来源于提供的《场景映射表》，绝对禁止自行捏造或同义词替换！
3. **动词与意图至上**：
   - 准备/设置类：动作涉及“登录/注册/账号/设置/打开/滑动查找”，优先归类为【系统/账户管理】等基础类。
   - 播放/浏览类：出现“播放/看/听”，归为【播放/查看】类。
   - 搜索/查询类：明确出现“搜索/找/查询”，归为【搜索】类。
4. **兜底**：若完全无法匹配，请将三个层级均设为 "Unclassified"，并在 reason 中说明原因。

# Output Requirements
请严格输出一个JSON对象，不要输出任何思考过程或前缀。格式严格如下：
```json
{{
  "scene": "一级场景名称或Unclassified",
  "capability": "二级场景名称或Unclassified",
  "sub_capability": "三级场景名称或Unclassified",
  "reason": "匹配逻辑简述"
}}```"""


def get_dependency_rel_and_task(task, current_app):
    return f"""# Role
你是一个高级 AI 数据标注专家，专门负责为训练 GUI Agent 构建高质量的“指令与前置状态”数据集。

# Context
为了让大模型学习任务之间的上下文因果关系，你需要对输入的【目标任务】进行“前置状态推演”。你需要判断：为了让目标任务合乎逻辑地发生，App 中必须提前存在什么历史数据或状态？并据此反向生成构造该状态的【客观前置任务描述】。

【数据标注红线（Critical）】
1. 为了保证数据集聚焦于 App 的核心业务能力，请默认所有场景下 App **已处于正常登录状态**。绝对不要生成“登录账号”、“注册账号”作为前置任务，这属于无效的脏数据！
2. **【反轨迹规则】**：生成的前置任务必须是单一的、高层的目标动作。**绝对禁止**包含具体的 UI 导航路径或中间操作步骤（如：“打开‘我的’页面”、“进入‘设置’”等）。只需描述最终构造出数据的那个动作。
# Input Data
## 待标注的目标任务 (Target Task)
- 任务描述：{task}
- 当前应用：{current_app}

# Reasoning Logic
请严格按照以下步骤进行推演，彻底区分“执行步骤”与“历史数据依赖”：

1. 意图拆解与依赖判断：
   - 【判定为 "zero"（无状态任务）】：如果任务可以直接从 App 首页开始执行，**不需要用户之前在 App 里留下过任何私有数据（如历史记录、已收藏列表、购物车商品等）**。
     ⚠️ 注意：导航、查找、搜索、甚至打开某个特定频道，都属于“执行步骤”，绝不是前置依赖！
     *触发词*：搜索、添加、收藏、点赞、购买、播放（某具体剧名）。
   - 【判定存在依赖（有状态任务）】：任务的执行**强依赖于用户过去产生的历史数据或特定状态**。如果没有这些历史数据，这个操作在 UI 上根本不成立（比如：没有点赞过，就无法取消点赞）。此时进入第 2 步。
    **【关键对比示例】**：
   - ⭕️ 任务“搜索某剧并添加到收藏夹” -> 包含创建步骤，一气呵成，判定为 "none"。
   - ❌ 任务“播放我收藏夹里的某部剧” -> 缺少创建步骤，依赖历史数据，进入依赖判定。
   - ⭕️ 任务“挑选一件商品加入购物车并结算” -> 动作链完整，判定为 "none"。
   - ❌ 任务“清空我的购物车” -> 依赖历史数据，进入依赖判定。

2. 依赖关系强弱判定（Critical）：
   - 【判定为 "weak"（弱依赖/需预造数据）】：任务需要特定的历史数据，且无需借助外部物理设备。你需要逆向推演：“要凭空产生这个必备的历史数据，必须先执行什么客观任务？”（例如：目标是“取消收藏某物”，前置客观任务必须是“搜索并收藏该物”）。
   - 【判定为 "strong"（强依赖/物理壁垒）】：该任务强依赖外部物理世界交互（如：接收短信验证码、扫码、绑定真实信用卡、人脸识别）。

3. 前置任务生成（仅在 dependency_type 为 "weak" 时）：
   - 必须生成一条**客观、简洁、动作明确且无 UI 导航路径**的单一 GUI 任务描述，任务描述中必须自然地**显式包含 App 名称**。

# Output Requirements
请严格输出一个JSON对象，不要输出任何思考过程或前缀。格式严格如下：
```json
{{
  "dependency_relationships": "zero/weak/strong",
  "pre_task": "如果为 weak，填写逆向推演出的前置任务；否则填写 null",
  "reason": "简述状态推演的核心逻辑"
}}```
"""


if __name__ == "__main__":
    from task_generate import call_api_model, inference_qwen3_vl_32b
    task = r'继续看刚在腾讯视频观看的电视剧'
    current_app = r'腾讯视频'
    system_prompt = 'You are a helpful assistant.'
    prompt = get_dependency_rel_and_task(task, current_app)
    for i in range(5):
        # res = inference_qwen3_vl_32b(system_prompt, prompt)
        res = call_api_model(prompt)
        print(f"第{i + 1}次：'\n'{res}")