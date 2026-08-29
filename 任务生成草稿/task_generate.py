import re
import time
import uuid
import os
import sys
from pathlib import Path

import pandas as pd
import random
import logging
from datetime import datetime
import json
import ast
import requests
import concurrent.futures

TASK_GENERATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TASK_GENERATION_DIR.parent
DEFAULT_KNOWLEDGE_BASE_DIR = PROJECT_ROOT / "backend_workspace" / "task_generation" / "KnowledgeBase"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.model_config import DEFAULT_ENV_FILE, load_env_values, load_model_config
from prompt import *


def _task_generation_config():
    """Resolve the task-generation model from the shared backend/.env file."""

    return load_model_config(DEFAULT_ENV_FILE, module="task_generation")


def _knowledge_base_path(name: str) -> Path:
    values = load_env_values(DEFAULT_ENV_FILE)
    configured_root = values.get("TASK_GENERATION_KNOWLEDGE_BASE_DIR", "")
    root = Path(configured_root).expanduser() if configured_root else DEFAULT_KNOWLEDGE_BASE_DIR
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root / name


def resolve_knowledge_base_path(path: str | Path) -> Path:
    """Resolve the repository's KnowledgeBase paths independently of cwd."""

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    if candidate.parts and candidate.parts[0].lower() == "knowledgebase":
        return _knowledge_base_path(candidate.name)
    return candidate


def _task_generation_worker_count(default: int) -> int:
    values = load_env_values(DEFAULT_ENV_FILE)
    raw = values.get("TASK_GENERATION_MAX_CONCURRENT") or os.environ.get(
        "TASK_GENERATION_MAX_CONCURRENT"
    )
    if not raw:
        return default
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"TASK_GENERATION_MAX_CONCURRENT must be a positive integer, got {raw!r}"
        ) from exc
    if workers < 1:
        raise ValueError(
            f"TASK_GENERATION_MAX_CONCURRENT must be a positive integer, got {workers}"
        )
    return workers


def _model_endpoint(config) -> str:
    return config.base_url.rstrip("/") + "/chat/completions"


def _post_model_json(payload: dict) -> dict:
    """Call the configured OpenAI-compatible endpoint with bounded retries."""

    config = _task_generation_config()
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    request_kwargs = {
        "headers": headers,
        "json": payload,
        "timeout": config.timeout,
        "verify": config.verify,
    }
    if config.proxy:
        request_kwargs["proxies"] = {"http": config.proxy, "https": config.proxy}
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    last_error: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            with requests.Session() as session:
                session.trust_env = config.trust_env
                response = session.post(_model_endpoint(config), **request_kwargs)
            if response.status_code == 200:
                try:
                    value = response.json()
                except ValueError as exc:
                    raise RuntimeError("model endpoint returned invalid JSON") from exc
                if not isinstance(value, dict):
                    raise RuntimeError("model endpoint returned a non-object response")
                return value
            last_error = RuntimeError(
                f"model endpoint returned HTTP {response.status_code}"
            )
            if response.status_code not in retryable_statuses:
                raise last_error
        except requests.RequestException as exc:
            last_error = exc
        if attempt < config.max_retries:
            time.sleep(min(2**attempt, 10))

    raise RuntimeError("model request failed after bounded retries") from last_error


def _response_text(value: dict) -> str:
    try:
        content = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("model response does not contain choices[0].message.content") from exc
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    if not isinstance(content, str):
        raise RuntimeError("model response content is not text")
    return content


def match_json_content(text):
    """解析json字符串"""
    match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    try:
        res_json = match.group(1)
        result = json.loads(res_json)
        return result
    except Exception as e:
        print(f"依赖判定模型解析json失败{e}")
        return None


def call_api_model(query):
    start_time = time.time()
    config = _task_generation_config()
    messages = [
        {
            "role": "user",
            "content": query,
        }
    ]
    payload = {
        "model": config.model,
        "messages": messages
    }
    response = _response_text(_post_model_json(payload)).split('</think>')[-1].strip()
    end_time = time.time()
    cost_time = end_time - start_time
    print(f"cost_time:{cost_time}")
    logging.info(f"调用任务生成模型耗时{cost_time}s")
    return response


def inference_qwen3_vl_32b(user_prompt: str, temperature=0.7, top_p=0.8, top_k=500, max_tokens=2048):
    start_time = time.time()
    config = _task_generation_config()

    user_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": user_prompt}
        ]
    }
    messages = [
        {
            "role": "system",
            "content": 'You are a helpful assistant.'
        },
        user_msg
    ]
    data = {
        "model": config.model,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "max_tokens": max_tokens,
        # "extra_body": {'repetition_penalty': repetition_penalty},
        "messages": messages
    }

    response = _response_text(_post_model_json(data))
    end_time = time.time()
    cost_time = end_time - start_time
    print(f"cost_time:{cost_time}")
    logging.info(f"调用任务生成模型耗时{cost_time}s")
    return response


def get_scene_tree_for_dependency():
    """为前置依赖任务prompt获取场景树"""
    scene_file = _knowledge_base_path('VLA场景树.xlsx')
    df = pd.read_excel(scene_file)
    df['formatted_scene'] = df.apply(
        lambda x: f"{x['scene']} > {x['capability']} > {x['sub_capability']} | 涵盖App：{x['target_app']}", axis=1
    )
    scene_tree_text = '\n'.join(df['formatted_scene'].tolist())
    return scene_tree_text


def process_vla_knowledge_flow(vla_path, control_kb_path, resource_kb_path, sample_num=1):
    """
    VLA场景数据流处理函数

    参数:
    - vla_path: VLA场景树 Excel 路径
    - control_kb_path: APP操控先验知识库 Excel 路径
    - resource_kb_path: APP资源先验知识库 Excel 路径
    - sample_num: 随机抽取资源的条数，默认为1

    返回:
    - pd.DataFrame: 包含合并信息及 resource_prior 字典列表的结果表
    """

    def _parse_to_list(x):
        """内部工具：处理各种格式的 App 列表字符串"""
        if pd.isna(x): return []
        if isinstance(x, list): return x
        if isinstance(x, str):
            x = x.strip()
            if x.startswith('[') and x.endswith(']'):
                try:
                    # 替换中文逗号并安全解析
                    return ast.literal_eval(x.replace('，', ','))
                except:
                    return [item.strip() for item in x.strip('[]').split(',')]
            return [x]
        return [str(x)]

    # 1. 加载数据
    vla_tree = pd.read_excel(resolve_knowledge_base_path(vla_path))
    app_control_kb = pd.read_excel(resolve_knowledge_base_path(control_kb_path))
    app_resource_kb = pd.ExcelFile(resolve_knowledge_base_path(resource_kb_path))
    # 2. 对两张表的 target_app 进行展开 (Explode)
    vla_df = vla_tree.copy()
    vla_df['target_app'] = vla_df['target_app'].apply(_parse_to_list)
    vla_expanded = vla_df.explode('target_app')

    control_df = app_control_kb.copy()
    control_df['target_app'] = control_df['target_app'].apply(_parse_to_list)
    control_kb_expanded = control_df.explode('target_app')

    # 3. 执行左连接
    # 核心字段匹配：场景、能力、子能力、具体APP
    merge_keys = ['scene', 'capability', 'sub_capability', 'target_app']
    merged_df = pd.merge(
        vla_expanded,
        control_kb_expanded[merge_keys + ['sub_capability_desc']],
        on=merge_keys,
        how='left'
    )
    # 填充描述缺失值
    merged_df['sub_capability_desc'] = merged_df['sub_capability_desc'].fillna('无')

    # 4. 预载资源库的所有 Sheet 减少 IO 开销
    resource_data = {sheet: pd.read_excel(app_resource_kb, sheet_name=sheet)
                     for sheet in app_resource_kb.sheet_names}
    # 5. 遍历处理资源先验
    final_data = []
    for _, row in merged_df.iterrows():
        item = row.to_dict()
        resource_prior = []

        # 逻辑判断：如果需要资源且该 APP 在资源库中有 Sheet
        if row.get('use_resource_prior') == True:
            app_name = str(row['target_app'])
            if app_name in resource_data:
                sheet_df = resource_data[app_name]
                if not sheet_df.empty:
                    # 确定实际抽取数量（不能超过总行数）
                    actual_n = min(len(sheet_df), sample_num)
                    samples = sheet_df.sample(n=actual_n)
                    # 转换为字典列表格式
                    resource_prior = samples.to_dict(orient='records')

        item['resource_prior'] = resource_prior
        final_data.append(item)
    return final_data


# 生成带时间的动态文件名
current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
log_dir = TASK_GENERATION_DIR / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_filename = str(log_dir / f"task_generation_{current_time}.txt")

logging.basicConfig(
    filename=log_filename,
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    encoding='utf-8'
)

logging.info(f"日志文件已创建: {log_filename}")


def parse_generated_tasks(raw_response):
    """
    将模型生成的 JSONL 字符串解析为 Python List
    """
    task_list = []

    # 1. 按行分割并去除首尾空格
    lines = raw_response.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 2. 尝试解析每一行
        try:
            # 兼容处理：如果模型不听话带了 ```json 标记，这里可以简单过滤
            if line.startswith('```') or line.endswith('```'):
                continue

            task_dict = json.loads(line)
            task_list.append(task_dict)
        except json.JSONDecodeError:
            # 如果某一行解析失败，打印出来方便调试，或者直接忽略
            logging.warning("模型返回中存在无法解析的任务行，已跳过。")

    return task_list


def classify_pre_task_scene(pre_task_text: str, current_app: str) -> dict:
    """根据场景树划分前置依赖的场景"""
    scene_tree_text = get_scene_tree_for_dependency()  # 获取完整的场景树文本
    prompt = get_sce_by_task(scene_tree_text, pre_task_text, current_app)

    raw_res = inference_qwen3_vl_32b(prompt)

    try:
        # 直接解析出扁平的字典
        res_json = match_json_content(raw_res)
        logging.info(f"当前已成功获取到了前置任务的场景分类：{res_json}")
        return {
            "scene": res_json.get("scene", "Unclassified"),
            "capability": res_json.get("capability", "Unclassified"),
            "sub_capability": res_json.get("sub_capability", "Unclassified")
        }
    except Exception as e:
        logging.error(f"前置任务场景分类解析失败: {e}。返回 Unclassified。")
        return {
            "scene": "Unclassified",
            "capability": "Unclassified",
            "sub_capability": "Unclassified"
        }


def check_and_generate_dependent_tasks(item: dict) -> list:
    """
    接收单条任务字典 item，判断并生成前置依赖，返回处理后的任务列表。
    【已适配全新合一版 Prompt，性能翻倍】
    """
    # 1. 为当前主任务提前生成唯一的UUID
    main_task_uuid = str(uuid.uuid4())

    # copy原字典中的数据，避免直接修改原字典
    main_task_item = item.copy()
    main_task_item['task_uuid'] = main_task_uuid

    # 获取任务文本及app名称
    task_text = item['task']
    app_name = item.get('app', '未知应用')

    try:
        # 2. 调用大模型：一次性完成“判断强弱”和“生成文本”
        check_depend_prompt = get_dependency_rel_and_task(task_text, app_name)
        raw_res = inference_qwen3_vl_32b(check_depend_prompt)

        # 防御性解析 JSON
        depend_info = match_json_content(raw_res)
        if not isinstance(depend_info, dict):
            logging.warning("⚠️ 模型未返回标准 JSON，退级为无依赖。")
            depend_info = {}

        # 安全提取依赖类型（默认当做 none 无依赖）
        dep_type = str(depend_info.get('dependency_relationships', 'zero')).strip().lower()

        # --- 分支 A：弱依赖（需要构造数据） ---
        if dep_type == 'weak':
            pre_task_text = depend_info.get('pre_task')

            # 确保大模型真的吐出了有效文本，而不是 "null" 或空字符串
            if pre_task_text and str(pre_task_text).lower() != 'null':
                logging.info(f"💡 成功提取 weak 前置任务: [{pre_task_text}]")

                pre_task_uuid = str(uuid.uuid4())

                # 构建【前置任务】
                pre_task_item = item.copy()
                pre_task_item['task'] = pre_task_text
                pre_task_item['task_uuid'] = pre_task_uuid

                # 调用独立的场景分类函数
                scene_dict = classify_pre_task_scene(pre_task_text, app_name)
                pre_task_item['scene'] = scene_dict.get('scene', 'Unclassified')
                pre_task_item['capability'] = scene_dict.get('capability', 'Unclassified')
                pre_task_item['sub_capability'] = scene_dict.get('sub_capability', 'Unclassified')

                # 专属标识
                pre_task_item['pre_dependency'] = 'pre_node'
                pre_task_item['pre_task_uuid'] = None

                # 更新【主任务】的指针
                main_task_item['pre_dependency'] = 'weak'
                main_task_item['pre_task_uuid'] = pre_task_uuid

                return [pre_task_item, main_task_item]
            else:
                logging.warning(f"⚠️ 判定为 weak，但 pre_task 为空。回退为无依赖。")

        # --- 分支 B：强依赖（物理壁垒） ---
        elif dep_type == 'strong':
            logging.warning(f"🚨 遇到强依赖任务: {task_text}")
            main_task_item['pre_dependency'] = 'strong'
            main_task_item['pre_task_uuid'] = None

            # 强依赖任务直接标记不可执行，在这里显式地覆盖为-2
            main_task_item['status'] = '-2'
            return [main_task_item]

    except Exception as e:
        # 捕获网络断开、正则提取失败等一切致命错误，保证主程序不崩
        logging.error(f"❌ 解析任务 [{task_text}] 时发生异常: {e}。强制回退为无依赖。")

    # --- 分支 C：无依赖 (zero) 或 兜底情况 ---
    # 代码走到这里，说明要么是 none，要么中间发生了异常
    main_task_item['pre_dependency'] = 'zero'
    main_task_item['pre_task_uuid'] = None

    return [main_task_item]


def process_single_task(row, app_name, generate_per_sub_capability):
    """
    处理单条任务记录的独立工作函数
    """
    current_row_app = row.get('target_app')

    # 【关键适配】过滤逻辑
    if app_name is not None and current_row_app != app_name:
        return []

    prompt = system_prompt(
        row['scene'],
        row['capability'],
        row['sub_capability'],
        row['sub_capability_desc'],
        current_row_app,
        row['resource_prior'],
        row['reference_example'],
        generate_per_sub_capability
    )

    # 构造任务标识符，方便并发时的日志追踪
    task_id = f"{current_row_app}的{row['scene']}场景-{row['capability']}能力-{row['sub_capability']}二级能力"
    logging.info(f"正在为 {task_id} 生成任务：")
    logging.info(f"--APP资源先验知识请求调用 {True if row['resource_prior'] else False}.")
    logging.info(f"--APP操纵先验知识请求调用 {True if row['sub_capability_desc'] else False}.")

    try:
        # 调用大模型 (I/O 密集型或计算密集型)
        res = inference_qwen3_vl_32b(prompt)
        logging.info(f"[{task_id}] 生成任务完成。")
        res_list = parse_generated_tasks(res)
        final_enhanced_tasks = []
        for item in res_list:
            after_check_item = check_and_generate_dependent_tasks(item)  # 这里可能返回1条或2条任务
            final_enhanced_tasks.extend(after_check_item)
        return final_enhanced_tasks

    except Exception as e:
        logging.error(f"[{task_id}] 生成任务时发生异常: {str(e)}")
        return []


def task_generate(
        app_name: str = None,
        scene: str = None,
        capability: str = None,
        sub_capability: str = None,
        generate_per_sub_capability=5,
        max_workers=None,
        progress_callback=None):
    task_list = []
    if max_workers is None:
        max_workers = _task_generation_worker_count(default=16)
    # 1. 获取完整的场景树知识库
    task_info = process_vla_knowledge_flow(
        _knowledge_base_path('VLA场景树.xlsx'),
        _knowledge_base_path('APP操控先验知识库.xlsx'),
        _knowledge_base_path('APP资源先验知识库.xlsx'),
        sample_num=generate_per_sub_capability
    )

    # 2. 【漏斗过滤】提前筛选出我们需要生成的“目标场景节点”
    filtered_task_info = []
    for row in task_info:
        # 兼容你的 Excel 列名，优先取 'target_app'
        current_app = row.get('target_app', row.get('app'))

        # 如果调用时传了限制条件，且当前行不匹配，则直接跳过
        if app_name and current_app != app_name:
            continue
        if scene and row.get('scene') != scene:
            continue
        if capability and row.get('capability') != capability:
            continue
        if sub_capability and row.get('sub_capability') != sub_capability:
            continue

        filtered_task_info.append(row)
    logging.info(f"🔍 经过条件过滤，共有 {len(filtered_task_info)} 个场景节点需要生成任务...")

    # 3. 拦截空跑：如果过滤后没有命中任何场景，直接返回，不启动线程池
    if not filtered_task_info:
        logging.warning("⚠️ 知识库中未找到匹配的场景条件，生成跳过。")
        return task_list

    # 4. 使用 ThreadPoolExecutor 进行并发执行
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 将任务提交给线程池
        futures = [
            executor.submit(process_single_task, row, app_name, generate_per_sub_capability)
            for row in filtered_task_info
        ]

        # as_completed 会在每个线程完成时立即产出结果，保持较高的响应性
        completed_nodes = 0
        for future in concurrent.futures.as_completed(futures):
            try:
                result_tasks = future.result()
                if result_tasks:
                    task_list.extend(result_tasks)
            except Exception as e:
                logging.error(f"获取并发结果时发生异常: {str(e)}")
            finally:
                completed_nodes += 1
                if progress_callback:
                    progress_callback(completed_nodes, len(filtered_task_info))

    return task_list
