import concurrent.futures
import json
import logging
import re
import secrets
import time

import pandas as pd
import os
from datetime import datetime
from task_generate import (
    process_vla_knowledge_flow,
    inference_qwen3_vl_32b,
    get_scene_tree_for_dependency,
    classify_pre_task_scene,
    resolve_knowledge_base_path,
    _task_generation_worker_count,
)
from prompt import get_sce_by_task
from pypinyin import pinyin, Style


def read_testcase(input_file_path):
    """读取原始测试集文件"""
    df = pd.read_excel(input_file_path, sheet_name=0)
    pass_testcase = []
    fail_testcase = []
    for idx, row in df.iterrows():
        if row.get('任务结果') == 'TRUE':
            pass_testcase.append(row.to_dict())
        else:
            fail_testcase.append(row.to_dict())
    return pass_testcase, fail_testcase


def match_scene_by_task(ipt_file):
    """按照330场景为测评集做场景匹配"""
    ori_df = pd.read_excel(ipt_file, sheet_name=0)
    result = []
    for idx, row in ori_df.iterrows():
        task = row.get('任务', None)
        app_name = row.get('涉及APP', None)
        sce_dict = classify_pre_task_scene(task, app_name)
        result.append({
            'app': app_name,
            'task': task,
            'scene': sce_dict.get('scene', None),
            'capability': sce_dict.get('capability', None),
            'sub_capability': sce_dict.get('sub_capability', None)
        })

    res_df = pd.DataFrame(result)
    with pd.ExcelWriter(ipt_file, mode='a', engine='openpyxl') as writer:
        res_df.to_excel(writer, sheet_name='新场景匹配', index=False)


def match_scene_by_task_new(input_list: list, progress_callback=None, max_workers=None):
    """按照 330 场景为测评集做场景匹配，并并行处理独立用例。"""
    if not input_list:
        return []
    if max_workers is None:
        max_workers = _task_generation_worker_count(default=32)

    def classify(index, row):
        task = row.get('任务', None)
        app_name = row.get('涉及APP', None)
        scene_dict = classify_pre_task_scene(task, app_name)
        return index, {
            'app': app_name,
            'task': task,
            'scene': scene_dict.get('scene', None),
            'capability': scene_dict.get('capability', None),
            'sub_capability': scene_dict.get('sub_capability', None)
        }

    result = [None] * len(input_list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(classify, index, row) for index, row in enumerate(input_list)]
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            index, item = future.result()
            result[index] = item
            if progress_callback:
                progress_callback(completed, len(input_list))
    return result


def get_chinese_initials(text):
    """
    提取字符串中中文字符的拼音首字母，并转换为大写
    对于英文字母默认保留
    """
    if not text:
        return ""

    py_list = pinyin(text, style=Style.FIRST_LETTER, strict=False)

    initials = ''.join([item[0].upper() for item in py_list if item[0].isalpha()])
    return initials


def generate_case_id(app, scene, short_uuid, seq_num):
    """
    根据规则生成单个用例编号
    格式：{scene首字母大写}-{app首字母大写}-{uuid}-{序号}
    """
    parts = re.split(r'--|-', str(scene))
    scene_prefix = parts[0].strip()

    scene_initial = get_chinese_initials(scene_prefix)
    app_initial = get_chinese_initials(app)

    return f"{scene_initial}-{app_initial}-{short_uuid}-{seq_num}"


def _find_scene_node(scene_tree_list: list, app: str, scene: str, cap: str, sub_cap: str) -> dict:
    """
    通用节点定位器：在场景树中通过四维坐标找到对应的完整字典。
    """
    if not scene_tree_list:
        return {}

    for node in scene_tree_list:
        # 进行四维坐标匹配
        if (node.get('target_app') == app and
                node.get('scene') == scene and
                node.get('capability') == cap and
                node.get('sub_capability') == sub_cap):
            return node
    return {}


def generate_flywheel_to_excel(failed_case: dict, generate_n: int = 5):
    """
    飞轮引擎：同步获取先验信息与参考示例，生成极具发散性的数据。
    """
    logging.info(f"🚨 捕获失败用例，开始深度检索上下文: {failed_case.get('task')}")

    scene_tree_list = process_vla_knowledge_flow(
        resolve_knowledge_base_path('KnowledgeBase/VLA场景树.xlsx'),
        resolve_knowledge_base_path('KnowledgeBase/APP操控先验知识库.xlsx'),
        resolve_knowledge_base_path('KnowledgeBase/APP资源先验知识库.xlsx'),
        sample_num=generate_n
        )
    # 1. 提取案发坐标
    app = failed_case.get('app', '未知App')
    scene = failed_case.get('scene', '未知场景')
    cap = failed_case.get('capability', '未知能力')
    sub_cap = failed_case.get('sub_capability', '未知子能力')
    failed_task = failed_case.get('task', '未知任务')

    # 2. 🎯 一次性获取该节点的所有先验数据
    node_data = _find_scene_node(scene_tree_list, app, scene, cap, sub_cap)
    # 安全提取各个字段，并提供默认兜底值
    app_prior = node_data.get('resource_prior', '无特定先验信息，请基于 App 常规逻辑自由发挥。')
    reference_example = node_data.get('reference_example', '暂无参考示例')
    sub_capability_desc = node_data.get('sub_capability_desc', '请参考三级子能力的字面意思执行。')

    # 3. 组装 Prompt (“资深移动端用户行为专家”模板)
    city = '上海'
    current_date = datetime.now().strftime('%Y-%m-%d')
    prompt = f"""
# Role
你是一名资深的移动端自动化测试架构师与数据构造专家，负责通过构造对抗性的变体任务（Variant Tasks）来修复 Agent 的能力缺陷。

# Context
- 【目标 App】: {app}
- 【业务场景】: {scene}
- 【一级能力】: {cap} 
- 【二级能力】: {sub_cap}
- 【二级能力参考】: {sub_capability_desc}
- 【失败的原始任务 (Failed Task)】: "{failed_task}"
- 【App 先验信息 (可选引用源)】:
{app_prior}
- 【参考示例】: "{reference_example}"

# Task
我们的智能体在执行【失败的原始任务】时发生了错误。请结合上述维度，为该 App 生成 {generate_n} 条具有发散性、口语化且符合逻辑的训练变体任务（Variant Tasks）。

# Requirements & Constraints
1. 🚫【红线：实体强制替换原则】（最高优先级）: 
   - 你必须在脑海中拆解原任务：保留【App名称】和【操作意图/动作骨架】，但**100% 替换掉所有的【具体内容实体】（如搜索词、分类名、人名、频道Tab名等）**。
   - 例如原任务中有“古装”、“爱情”、“悬疑”或“张译”，变体任务中绝对不能再出现这些词及同义词！必须替换为同类的其他词汇（如改为“战争”、“胡军”等）。
2. 🎯【条件化实体抽取】: 替换的新实体必须且仅能从【App 先验信息】中随机选取，或参考【二级能力参考】。严禁捏造名单外的实体。如果没有提供先验名单，请自行思考一个同领域的不同实体。
3. 🎭【多维变异策略】: 
   - 严禁机械复读参考示例或原任务的动词。
   - 改变句式结构（倒装、省略等），利用丰富的同义词替换操作动词（如把“搜索”替换为“帮我找找”、“我想看”）。
   - 赋予用户主观诉求的语气，例如“最近剧荒，帮我...”。
4. 📍【隐含起点与环境】: 任务起点默认为【进入 App 首页后】；指令中必须自然地包含“{app}”名称。
5. ✅【逻辑一致性】: 变体任务在宏观意图上必须受限于当前的【二级能力】范畴，严禁发散到其他页面功能。
# Output Format
请直接输出 JSON 数组格式（包含 {generate_n} 个对象），不要包含 Markdown 代码块标记（如 ```json）。
输出格式：
[
  {{ "task": "生成的变体任务描述1" }},
  {{ "task": "生成的变体任务描述2" }}
]
"""

    prompt_v1 = f"""# Role
    你是一名资深的移动端自动化测试架构师与数据构造专家，负责通过构造对抗性的变体任务（Variant Tasks）来修复 Agent 的能力缺陷。

    # Context
    - 【目标 App】: {app}
    - 【业务场景】: {scene}
    - 【一级能力】: {cap} 
    - 【二级能力】: {sub_cap}
    - 【二级能力参考】: {sub_capability_desc}
    - 【定位城市】: {city}
    - 【当前日期】: {current_date}
    - 【失败的原始任务 (Failed Task)】: "{failed_task}"
    - 【App 先验信息 (可选引用源)】:
    {app_prior}
    - 【参考示例】: "{reference_example}"

    # Task
    我们的智能体在执行【失败的原始任务】时发生了错误。请结合上述维度（特别是当前的时空背景），为该 App 生成 {generate_n} 条具有发散性、口语化且符合逻辑的训练变体任务（Variant Tasks）。

    # Requirements & Constraints
    1. 🚫【红线：实体强制替换原则】（最高优先级）: 
       - 你必须在脑海中拆解原任务：保留【App名称】和【操作意图/动作骨架】，但**100% 替换掉所有的【具体内容实体】（如搜索词、分类名、人名、频道Tab名等）**。
       - 例如原任务中有“古装”、“爱情”或“张译”，变体任务中绝对不能再出现这些词及同义词！必须替换为同类的其他词汇（如改为“战争”、“胡军”等）。
    2. 🎯【条件化实体抽取】: 替换的新实体必须且仅能从【App 先验信息】中随机选取，或参考【二级能力参考】。严禁捏造名单外的实体。如果没有提供先验名单，请自行思考一个同领域的不同实体。
    3. 🗺️【时空语境与地域锚定】: 
       - **相对时间转化**: 评估业务是否需要时间属性，若需要，请将【当前日期】转化为“今天”、“最近”、“明晚”等生活化相对表达，拒绝生硬填入日期。
       - **城市内的精细 POI 构造**: 【定位城市】是地理边界（如“西安”）。当业务场景（如出行打车、本地团购、导航等）需要具体地点时，你必须基于该城市，合理提取或构造符合该城实际情况的**具体兴趣点（POI）**（如知名商圈、景点、大学、街道名等）。例如，定位是西安时，任务中应自然出现“钟楼”、“大雁塔”或“高新区”等地名，**严禁**出现跨城市或违背地理常识的地点（如在西安找东方明珠）。
    4. 🎭【多维变异策略】: 
       - 严禁机械复读参考示例或原任务的动词。
       - 改变句式结构（倒装、省略等），利用丰富的同义词替换操作动词。
       - 赋予用户主观诉求的语气，结合地点设定场景（例如：“我现在在{city}的[某商圈]逛街，用{app}帮我找个...”）。
    5. 📍【隐含起点与环境】: 任务起点默认为【进入 App 首页后】；指令中必须自然地包含“{app}”名称。
    6. ✅【逻辑一致性】: 变体任务在宏观意图上必须受限于当前的【二级能力】范畴，严禁发散到其他页面功能。

    # Output Format
    请直接输出 JSON 数组格式（包含 {generate_n} 个对象），不要包含 Markdown 代码块标记（如 ```json）。
    输出格式：
    [
      {{ "task": "生成的变体任务描述1" }},
      {{ "task": "生成的变体任务描述2" }}
    ]"""
    prompt_v2 = f"""# Role
    你是一名资深的移动端自动化测试架构师与数据构造专家，负责通过构造对抗性的变体任务（Variant Tasks）来修复 Agent 的能力缺陷。

    # Context
    - 【目标 App】: {app}
    - 【业务场景】: {scene}
    - 【一级能力】: {cap} 
    - 【二级能力】: {sub_cap}
    - 【二级能力参考】: {sub_capability_desc}
    - 【当前日期】: {current_date}
    - 【失败的原始任务 (Failed Task)】: "{failed_task}"
    - 【App 先验信息 (可选引用源)】:
    {app_prior}
    - 【参考示例】: "{reference_example}"

    # Task
    我们的智能体在执行【失败的原始任务】时发生了错误。请结合上述维度（特别是当前的时空背景），为该 App 生成 {generate_n} 条具有发散性、口语化且符合逻辑的训练变体任务（Variant Tasks）。

    # Requirements & Constraints
    1. 🚫【红线：实体强制替换原则】（最高优先级）: 
       - 你必须在脑海中拆解原任务：保留【App名称】和【操作意图/动作骨架】，但**100% 替换掉所有的【具体内容实体】（如搜索词、分类名、人名、频道Tab名等）**。
       - 例如原任务中有“古装”、“爱情”或“张译”，变体任务中绝对不能再出现这些词及同义词！必须替换为同类的其他词汇（如改为“战争”、“胡军”等）。
    2. 🎯【条件化实体抽取】: 替换的新实体必须且仅能从【App 先验信息】中随机选取，或参考【二级能力参考】。严禁捏造名单外的实体。如果没有提供先验名单，请自行思考一个同领域的不同实体。
    3. 🗺️【时空语境与数值离散化】: 
       - **时间边界**: 必须严格控制在 2024年7月1日 至 2026年6月11日 范围内。
       - **🔴 表达格式 (严格限定)**: 必须采用明确的起止日期边界，例如“X年X月X日到Y年Y月Y日”。绝对禁止使用“最近”、“上半年”、“上个月”等模糊周期，也禁止使用没有结束时间的单点时间。
       - **🟢 数据随机要求 (反同质化)**: 严禁连续生成相同规律的日期段。每次生成时间范围时，必须执行以下随机策略：
         1. **起始日随机**: 打破总是从1号或15号开始的惯性。起始日必须是 1 到 31 之间的任意散乱数字（如 4号、17号、23号、29号）。
         2. **时间跨度随机**: 起止日期之间的天数不要固定为10天。请在 2天 到 90天 之间随机波动（例如：查跨度为3天的、跨度为18天的、跨度为47天的）。
         3. **跨月/跨年随机**: 交替生成同月内的时间段、跨月份的时间段，以及跨年份的时间段。
       - **示例**:
         - ❌ 错误（规律太死板，总是1号到10号）: "查一下2025年5月1日到2025年5月10日的门诊记录"
         - ✅ 正确（起始日散乱，跨度随机）: "查一下2024年8月14日到2024年9月3日的门诊记录"
         - ✅ 正确（起始日散乱，跨度随机）: "我需要在健康云里调取2025年11月22日到2025年11月27日的门诊数据"
    4. 🎭【多维变异策略】: 
       - 严禁机械复读参考示例或原任务的动词。
       - 改变句式结构（倒装、省略等），利用丰富的同义词替换操作动词。
    5. 📍【隐含起点与环境】: 任务起点默认为【进入 App 首页后】；指令中必须自然地包含“{app}”名称。
    6. ✅【逻辑一致性】: 变体任务在宏观意图上必须受限于当前的【二级能力】范畴，严禁发散到其他页面功能。

    # Output Format
    请直接输出 JSON 数组格式（包含 {generate_n} 个对象），不要包含 Markdown 代码块标记（如 ```json）。
    输出格式：
    [
      {{ "task": "生成的变体任务描述1" }},
      {{ "task": "生成的变体任务描述2" }}
    ]"""

    try:
        # 4. 调用大模型 (此处替换为你真实的 API 调用)
        response_text = inference_qwen3_vl_32b(prompt)
        # 解析逻辑保持不变
        start_idx = response_text.find('[')
        end_idx = response_text.rfind(']') + 1
        json_str = response_text[start_idx:end_idx]
        new_tasks_data = json.loads(json_str)

        # 5. 组装 Excel 记录，加入更多对照列
        flywheel_records = []
        short_uuid = secrets.token_hex(3)
        for index, item in enumerate(new_tasks_data, start=1):
            # 动态调用函数，传入当前的 app, scene 和 index 生成编号
            current_case_id = generate_case_id(app, scene, short_uuid, index)

            flywheel_records.append({
                "用例编号": current_case_id,
                "源失败任务": failed_task,
                "app": app,
                "scene": scene,
                "capability": cap,
                "sub_capability": sub_cap,
                "生成的变体任务": item.get("task", ""),
                "run": "flywheel",
                "审核状态": "待人工Review"
            })
        return flywheel_records

    except Exception as e:
        logging.error(f"❌ 飞轮导出流程异常: {e}")
        return None


def process_flywheel_export(ipt_file: str, output_file: str, max_workers=None, generate_n: int = 10, progress_callback=None):
    df = pd.read_excel(ipt_file, sheet_name='新场景匹配')
    df_list = df.to_dict(orient='records')

    # 1. 初始化一个空列表，用于汇总所有线程返回的字典
    all_generated_records = []

    if max_workers is None:
        max_workers = _task_generation_worker_count(default=32)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 2. 最佳实践：将 future 对象与原始 item 映射起来，方便错误追踪
        future_to_item = {
            executor.submit(generate_flywheel_to_excel, item, generate_n=generate_n): item
            for item in df_list
        }

        for completed, future in enumerate(concurrent.futures.as_completed(future_to_item), start=1):
            item = future_to_item[future]  # 获取当前 future 对应的原始输入数据
            try:
                # 获取结果。注意：如果内部涉及模型调用，10秒可能容易触发超时，建议视情况放宽
                result_list = future.result(timeout=30)

                # 3. 如果成功返回了列表，使用 extend 将其平铺追加到总列表中
                if result_list:
                    all_generated_records.extend(result_list)

            except concurrent.futures.TimeoutError:
                # 可以明确记录是哪个 app/scene 超时了
                app_name = item.get('app', 'Unknown')
                logging.error(f"任务超时: 提取 {app_name} 数据未能在规定时间内完成")
            except Exception as e:
                app_name = item.get('app', 'Unknown')
                logging.error(f"线程执行出错 (App: {app_name}): {e}")
            finally:
                if progress_callback:
                    progress_callback(completed, len(df_list))

    # 4. 循环结束后，将汇总的数据写入 Excel
    if all_generated_records:
        # 将列表字典转换为 DataFrame
        result_df = pd.DataFrame(all_generated_records)

        # 写入 Excel，index=False 避免将 DataFrame 的索引写入文件
        result_df.to_excel(output_file, index=False)
        logging.info(f"处理完成！成功将 {len(all_generated_records)} 条数据写入 {output_file}")
    else:
        logging.warning("未能生成任何有效数据，跳过 Excel 写入。")


if __name__ == "__main__":
    # ================ 测评集失败用例匹配新场景 ================
    input_file = r"D:\26job\7.24数据飞轮\扩写所需表格\7.24数据飞轮下一轮迭代扩写种子任务.xlsx"
    match_scene_by_task(input_file)
    process_flywheel_export(input_file, r"D:\26job\7.24数据飞轮\扩写所需表格\非Top场景第一轮迭代种子任务_扩写.xlsx")
