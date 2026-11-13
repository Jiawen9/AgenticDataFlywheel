from __future__ import annotations

import json
import logging
import os
import re

import config
from agent.evaluator.gold_checklist import load_gold_checklist
from agent.evaluator.tools.registry import EvaluatorContext

logger = logging.getLogger(__name__)


def generate_verification_checklist(ctx: EvaluatorContext, args: dict) -> dict:
    from frozen_checklist import apply as apply_frozen_checklist
    frozen = apply_frozen_checklist(ctx)
    if frozen is not None:
        return frozen
    # Prefer the trajectory's original task for gold lookup.  The evaluator
    # model is allowed to repeat the task in tool args, but it may paraphrase
    # it; a paraphrase must not accidentally disable an available gold spec.
    trajectory_task = ctx.trajectory.get("task", "")
    task = trajectory_task or args.get("task", "")

    # Human-authored task specs are the authoritative acceptance chain.  This
    # branch deliberately comes before the text_llm check: a gold task must be
    # evaluable even in an offline/unit-test process with no text client, and
    # most importantly must not spend a call regenerating its checklist.
    llm_only = getattr(ctx, "_checklist_policy", "auto") == "llm"
    gold = None if llm_only else load_gold_checklist(trajectory_task or task, ctx.trajectory)
    if gold:
        items = gold["checklist"]
        ctx.checklist = items
        ctx.checklist_source = "gold"
        ctx.checklist_metadata = {
            key: value for key, value in gold.items() if key != "checklist"
        }
        logger.info(
            "[checklist] loaded gold checklist task_id=%s items=%d match=%s",
            gold.get("task_id"), len(items), gold.get("match_method"),
        )
        return {
            "checklist": items,
            "count": len(items),
            "source": "gold",
            "metadata": ctx.checklist_metadata,
        }

    model = getattr(config, "EVALUATOR_TEXT_MODEL", "qwen3.8-flash")
    text_llm = ctx._text_llm
    if text_llm is None:
        return {"error": "text_llm not set on context"}

    system = _checklist_system_prompt()
    category = _detect_task_category(task)

    # Build key-elements section (from xlsx "页面关键元素" column).
    # Semantics: these are MUST-SATISFY acceptance conditions — the checklist must
    # cover every listed element (it may refine a broad one into verifiable checks,
    # but may NOT drop it), and the evaluator may ADD extra checks beyond them.
    key_elements = getattr(ctx, "key_elements", "") or ""
    ke_parsed = _parse_key_elements(key_elements)
    key_elements_section = ""
    if ke_parsed:
        ke_bullets = []
        for i, ke in enumerate(ke_parsed, 1):
            tag = f"priority_{ke['priority']}" if ke.get("priority") else f"人工标注要素{i}"
            ke_bullets.append(f"- {tag}：{ke['text']}")
        key_elements_section = (
            "## 页面关键元素（来自测试用例表 · **必须满足**）\n\n"
            "以下为人工按测试用例标注的「页面关键元素」，是判定任务成功所**必须满足的硬性验收条件**。\n\n"
            + "\n".join(ke_bullets) + "\n\n"
            "### 使用规则\n"
            "1. **逐条覆盖，不可遗漏**：上面每一条都必须有对应的 checklist item 覆盖"
            "（可一条 item 覆盖多条，也可多条 item 共同覆盖一条），**任何一条都不能被丢弃**。\n"
            "2. **宽泛条目应细化而非删除**：若某条过于宽泛、缺少区分度（如「当前页面正在播放电视剧」），"
            "需结合 task 中的具体信息（剧名 / 主演 / 榜单名 / 期数等）把它细化为**可验证的具体条件**，"
            "但必须保留并覆盖其原意。\n"
            "3. **可额外增加**：在上述必查关键元素之外，你认为还应当检查的条件"
            "（如搜索路径、中间页面、筛选步骤等）可以继续添加 item。\n"
            "4. 每一条 item 都必须是正向断言、包含可观察的具体 UI 条件。\n\n"
        )

    user_msg = (
        f"Task: {task}\nTask Category: {category}\n\n"
        + key_elements_section
        + "Generate the verification checklist."
    )
    messages = [{"role": "user", "content": user_msg}]

    # ── Attempt 1: first LLM call ──────────────────────────────────────────
    raw = _call_checklist_llm(text_llm, model, system, messages)
    items = _parse_checklist_json(raw)
    items = _validate_generated_items(items)

    # ── C3: retry up to 3 times on parse failure ───────────────────────────
    retry = 0
    while items is None and retry < 3:
        retry += 1
        logger.warning("[checklist parse] retry %d/3, raw len=%d", retry, len(raw))
        retry_msg = (
            f"Your previous output could NOT be parsed as valid JSON. Common error: "
            f"do NOT put string keys before objects inside an array. "
            f"WRONG: [{{...}}, \"chk_02\": {{...}}]  RIGHT: [{{...}}, {{...}}]\n\n"
            f"Your previous output was:\n{raw[:800]}\n\n"
            f"Please re-output the checklist as a VALID JSON array. Start with [ and end with ]. "
            f"Each element is a plain JSON object: "
            f'{{"id":"chk_NN","description":"...","check_method":"...","target_keywords":[...]}}\n\n'
            f"Re-generate the verification checklist for: {task}"
        )
        raw = _call_checklist_llm(text_llm, model, system, [{"role": "user", "content": retry_msg}])
        items = _parse_checklist_json(raw)
        items = _validate_generated_items(items)

    # ── C4: fallback heuristic checklist ───────────────────────────────────
    if items is None:
        return {"error": "checklist_generation_failed", "fatal": True}

    # ── C5: key-element coverage guarantee ─────────────────────────────────
    # The checklist LLM can silently drop a 页面关键元素 (e.g. HG-20 dropped
    # priority_2「黄宥天主演版本」). Any key element not covered by an item is
    # appended as a deterministic mandatory item so it ALWAYS must pass.
    items = _enforce_key_element_coverage(items, ke_parsed)

    for item in items:
        item.setdefault("checked", False)
        item.setdefault("result", None)
        item.setdefault("evidence", None)
    ctx.checklist = items
    ctx.checklist_source = "llm"
    ctx.checklist_metadata = {"schema": "evaluator-checklist/v1"}
    return {
        "checklist": items,
        "count": len(items),
        "source": "llm",
        "metadata": ctx.checklist_metadata,
    }


def _validate_generated_items(items: object) -> list | None:
    """A parsed but empty/malformed checklist is not a generated acceptance test."""
    if not isinstance(items, list) or not items:
        return None
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            return None
        key = item.get("id")
        description = item.get("description")
        if (not isinstance(key, str) or not key.strip() or key in seen
                or not isinstance(description, str) or not description.strip()):
            return None
        seen.add(key)
        copied = dict(item)
        # Generation cannot supply its own pass/fail evidence.
        copied.update(checked=False, result=None, evidence=None)
        result.append(copied)
    return result


# --------------------------------------------------------------------------- #
# 页面关键元素 (xlsx) — mandatory checklist coverage
# --------------------------------------------------------------------------- #
_KEY_LINE_RE = re.compile(r'^\s*priority_?(\d*)\s*[:：]?\s*[\[〔(（]?(.*?)[\]〕)）]?\s*$')
_ENTITY_RE = re.compile(r'[《「“]([^》」”]{1,60})[》」”]|"([^"]{1,60})"')
_NORM_SKIP = set('，。、；：？！（）()[]【】〔〕《》「」“”‘’"\'<>{}|/\\·—… \t\r\n')


def _parse_key_elements(ke_str: str) -> list[dict]:
    """Parse xlsx 页面关键元素 text into per-priority structured requirements.

    Each line looks like ``priority_1：[当前界面播放的是「攻略苗疆少年」的短剧]``
    → {"priority": "1", "text": "当前界面播放的是「攻略苗疆少年」的短剧", "entities": ["攻略苗疆少年"]}.
    """
    out: list[dict] = []
    if not ke_str or not ke_str.strip():
        return out
    seen_norm: set[str] = set()
    for line in ke_str.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _KEY_LINE_RE.match(s)
        if m:
            prio, content = m.group(1), m.group(2).strip()
        else:
            prio, content = "", s.strip()
        if not content:
            continue
        entities = [g for g in _ENTITY_RE.findall(content) for g in g if g]
        norm = _norm_text(content)
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        out.append({"priority": prio, "text": content, "entities": entities})
    return out


def _enforce_key_element_coverage(items: list, ke_parsed: list[dict]) -> list:
    """Guarantee every 页面关键元素 is covered by a checklist item.

    Prompt-only coverage is unreliable (the checklist LLM can silently drop a
    requirement). Coverage is checked ONLY against the LLM's own items (never against
    a previously-appended mandatory item, which could mask a second requirement via a
    shared generic phrase). Any uncovered key element is appended as a deterministic
    mandatory item (kp_NN) so it is always part of the checklist and must pass.
    """
    if not ke_parsed:
        return items
    base_items = list(items)  # coverage is judged ONLY against the LLM's own items
    out = list(items)
    used_ids = {it.get("id") for it in out if it.get("id")}
    base = 1
    for ke in ke_parsed:
        if _key_element_covered(ke, base_items):
            continue
        mand = _mandatory_item_for(ke, base)
        while mand["id"] in used_ids:
            base += 1
            mand = _mandatory_item_for(ke, base)
        used_ids.add(mand["id"])
        out.append(mand)
        base += 1
        logger.warning("[checklist coverage] key element NOT covered by LLM checklist — "
                       "appended mandatory item %s: %s", mand["id"], ke["text"][:80])
    return out


def _key_element_covered(ke: dict, items: list) -> bool:
    """True if any item's text references this key element.

    Distinctive key elements (those carrying a quoted entity or a salient token such as
    a name — e.g. priority_2「黄宥天主演版本」) must be *pinned*: some item must contain
    that entity / token (or the full text), otherwise the LLM dropped the requirement.
    Only generic key elements with no distinctive content (e.g. 「当前页面正在播放电视剧」)
    may fall back to a loose ≥4-char n-gram overlap to accept an LLM refinement.
    """
    ken = _norm_text(ke.get("text") or "")
    if not ken:
        return True  # empty requirement → trivially covered
    entities = [e for e in (ke.get("entities") or []) if _norm_text(e)]
    tokens = [t for t in _salient_tokens(ke.get("text") or "") if len(t) >= 2]
    distinctive = bool(entities) or bool(tokens)
    for it in items:
        tn = _norm_text(_item_text(it))
        if not tn:
            continue
        if any(e and _norm_text(e) in tn for e in entities):
            return True
        if any(_norm_text(t) in tn for t in tokens):
            return True
        if ken in tn:
            return True
        if not distinctive and _ngram_overlap(ken, tn, 4):
            return True
    return False


def _mandatory_item_for(ke: dict, n: int = 1) -> dict:
    """Deterministic checklist item for a key element that the LLM dropped."""
    prio = ke.get("priority") or ""
    tag = f"priority_{prio}" if prio else "人工关键元素"
    content = (ke.get("text") or "").strip()
    kws = list(ke.get("entities") or [])
    if not kws:
        kws = _salient_tokens(content)
    if not kws:
        kws = [content[:24]]
    return {
        "id": f"kp_{n:02d}",
        "description": f"【{tag}·必查】{content}",
        "check_method": "screenshot",
        "target_keywords": kws[:6],
        "checked": False,
        "result": None,
        "evidence": None,
    }


_SPLIT_STOP = (
    "当前页面正在播放", "当前界面正在播放", "当前正在播放", "正在播放", "播放的是",
    "播放了", "播放", "展示的", "展示", "搜索", "筛选", "查找", "已选中", "的", "是",
    "了", "在", "上", "一部", "一个", "一条", "且", "并", "与", "和", "关于", "主题",
    "列表", "结果", "内容", "相关", "版本", "主演", "剧集", "电视剧", "综艺", "电影",
    "短剧", "歌曲", "频道", "榜单", "资源", "系列", "页面", "界面", "当前", "优先",
    "必须", "满足", "成功", "最终",
)


def _salient_tokens(text: str) -> list[str]:
    """Pull distinctive tokens (names / values) out of a key element's text.

    Generic boilerplate (「当前页面正在播放」「播放的是」…) is stripped; the leftovers
    are the concrete constraints the checklist must reference (e.g. 黄宥天 from
    「播放的是黄宥天主演的版本」). Empty result ⇒ the key element is a generic condition.
    """
    stops = sorted(_SPLIT_STOP, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(s) for s in stops))
    segs = [seg for seg in pattern.split(_norm_text(text)) if len(seg) >= 2]
    return segs[:6]


def _item_text(it: dict) -> str:
    parts = [
        str(it.get("description") or ""),
        " ".join(str(k) for k in (it.get("target_keywords") or [])),
        str(it.get("visual_check") or ""),
    ]
    ui = it.get("ui_check")
    if isinstance(ui, dict):
        parts.append(str(ui.get("keyword") or "") + " " + str(ui.get("expected") or ""))
    return " ".join(parts)


def _norm_text(s: str) -> str:
    return "".join(ch for ch in str(s) if ch not in _NORM_SKIP and not ch.isspace())


def _char_ngrams(s: str, n: int) -> set[str]:
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _ngram_overlap(a: str, b: str, n: int) -> bool:
    if len(a) < n or len(b) < n:
        return False
    return bool(_char_ngrams(a, n) & _char_ngrams(b, n))


def _call_checklist_llm(text_llm, model: str, system: str, messages: list) -> str:
    resp = text_llm.chat(
        model=model,
        system=system,
        messages=messages,
        temperature=0.0,
        role="evaluator",
        phase="text",
    )
    return resp.get("content", "").strip()


def _load_checklist_rules() -> str:
    """Load checklist-relevant evaluation rules from evaluator_skill JSON files.

    Only loads rules whose IDs are relevant to checklist generation (not all rules).
    """
    import config as _cfg
    from pathlib import Path as _Path

    configured = os.environ.get("GUI_AGENT_EVALUATOR_RULES_DIR", "")
    skill_dir = _Path(configured) if configured else _cfg.PROJECT_ROOT / "agent" / "evaluator" / "evaluator_skill"
    if not skill_dir.is_dir() and os.environ.get("GUI_AGENT_EVALUATOR_STRICT_RULES") != "true":
        return ""

    checklist_rule_ids = {
        "query_empty_result_rule",
        "path_verification",
        "playback_judgment",
        "booking_completion_rule",
    }
    if os.environ.get("GUI_AGENT_EVALUATOR_STRICT_RULES") == "true":
        from evaluation_profiles import load_rules
        return "\n\n".join(rule["prompt_text"] for rule in load_rules(skill_dir)
                           if rule["id"] in checklist_rule_ids)

    parts: list[str] = []
    for f in sorted(skill_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for rule in data.get("rules", []):
                rid = str(rule.get("id", "")).strip()
                if rid in checklist_rule_ids:
                    text = str(rule.get("prompt_text", "")).strip()
                    if text:
                        parts.append(text)
        except Exception:
            pass
    return "\n\n".join(parts)


def _detect_task_category(task: str) -> str:
    """Classify task into a coarse category for targeted checklist guidance."""
    t = task

    booking_kw = ["预约", "挂号"]
    query_kw = ["查看", "收藏", "记录", "订单", "历史", "浏览", "我的收藏",
                "我的预约", "我的订单", "播放记录", "浏览足迹", "健康档案"]
    playback_kw = ["播放", "看", "观看", "追剧", "追", "放"]
    filter_kw = ["筛选", "过滤", "选择", "标签", "分类", "查找"]
    navigate_kw = ["打开", "进入", "找到", "去"]

    for kw in booking_kw:
        if kw in t:
            return "booking"
    for kw in query_kw:
        if kw in t:
            return "query"
    for kw in playback_kw:
        if kw in t:
            return "playback"
    for kw in filter_kw:
        if kw in t:
            return "filter"
    for kw in navigate_kw:
        if kw in t:
            return "navigation"
    return "action"


def _checklist_system_prompt() -> str:
    rules_text = _load_checklist_rules()
    rules_section = ""
    if rules_text:
        rules_section = (
            "## Special Evaluation Rules (MUST respect when generating checklist)\n"
            + rules_text + "\n\n"
        )

    return (
        "You are a task decomposition expert. Given a GUI task description, produce a "
        "structured JSON checklist of VERIFIABLE conditions that must all be true for the "
        "task to be considered successfully completed.\n\n"
        + rules_section +
        "## CRITICAL: Distinguish VIEW vs ACTION tasks\n"
        "Words like 「收藏」「预约」「评论」「关注」「点赞」 can be VIEW or ACTION:\n"
        "- VIEW: 「查看收藏」「查看我的预约」「浏览记录」「查看订单」→ agent only needs to\n"
        "  NAVIGATE to the target page. Verify the page was reached, NOT whether data exists.\n"
        "  Data may be empty because the user never saved/ordered/bookmarked anything.\n"
        "  WRONG: 「列表中展示至少一个已收藏的节目卡片」\n"
        "  RIGHT: 「进入了收藏/我的收藏节目页面」\n"
        "- ACTION: 「收藏第X部剧」「预约挂号」「发表评论」「关注某人」「点赞某内容」\n"
        "  → agent must PERFORM the terminal action. Generate a checklist item that verifies\n"
        "  the action RESULT (e.g. 确认已收藏/已预约/评论已发表/关注成功).\n"
        "  For ACTION tasks, do NOT stop at navigation — verify the action was executed.\n"
        "  Example: 「收藏列表中的第2部电视剧」→ must include an item verifying the\n"
        "  collection action was performed (e.g. 确认已成功收藏/预约该剧).\n\n"
        "## CRITICAL: Booking/Appointment Task Checklist Rules\n"
        "For tasks involving 「预约」(appointment), 「挂号」(registration) as ACTION:\n"
        "- Arriving at the confirmation page with correct info = PASSED.\n"
        "- CAPTCHA, SMS verification, payment are app security — agent can't complete them.\n"
        "- Only verify: correct hospital/department/date/doctor displayed on confirmation page.\n\n"
        "Rules:\n"
        "- Only produce POSITIVE assertions (what SHOULD be present).\n"
        "- Each item must have: id, description, check_method, target_keywords.\n"
        "- check_method is one of: screenshot, ui_tree, combined.\n"
        "- Use 'screenshot' for visual checks (e.g. 'page shows hotel cards').\n"
        "- Use 'ui_tree' for structural checks (e.g. 'checkbox X is checked').\n"
        "- Use 'combined' when both are needed (e.g. 'filter label visible AND checkbox toggled').\n"
        "- For combined items, also include 'ui_check' (keyword + expected attr) and 'visual_check' fields.\n"
        "- Return ONLY valid JSON array. No markdown fences, no explanation.\n"
        "- NEVER use ASCII double-quote (U+0022) inside string values. Use 「」 instead.\n"
        '  WRONG: \"shows \\\"transfer\\\" button\"  RIGHT: \"shows 「transfer」 button\"\n'
        "- CRITICAL: Output a JSON ARRAY (starting with [ and ending with ]). "
        "Each element is a plain object. Do NOT wrap objects in string keys.\n"
        '  WRONG: [{"id":"chk_01",...}, "chk_02": {"id":"chk_02",...}]\n'
        '  RIGHT: [{"id":"chk_01",...}, {"id":"chk_02",...}]\n'
        "## GENERAL RULES:\n"
        "1. **Only generate KEY verification items**: Each checklist item must verify\n"
        "   a CRITICAL condition — target page reached, key filter/setting applied,\n"
        "   or target state achieved. Do NOT generate trivial items like:\n"
        "   'App is open', 'Entered main interface', 'Navigated to section'.\n"
        "   These are always passed and waste evaluation time.\n"
        "2. **Navigation path matters**: If the task requires finding content through\n"
        "   a SPECIFIC list/ranking/tab (e.g. '热搜榜', '筛选 2023年'), generate\n"
        "   a SEPARATE checklist item verifying that the agent VISITED that page.\n"
        "   Do NOT rely on labels on the final detail page.\n"
        "3. For PLAYBACK tasks: generate items verifiable via (a) correct title,\n"
        "   (b) episode/season list with active selection, (c) play/pause controls,\n"
        "   or (d) '已看X集' viewing progress text. VIP pages and ads are normal.\n"
        'Format: [{"id":"chk_01","description":"...","check_method":"...","target_keywords":[...]}, ...]'
    )


def _fallback_checklist(task: str) -> list:
    """Generate a minimal checklist from task description keywords when LLM fails."""
    items = []
    idx = 1
    keyword_map = [
        (["打开", "启动", "进入"], "应用已打开并进入目标页面"),
        (["搜索", "查找", "输入"], "搜索操作已完成且结果可见"),
        (["筛选", "过滤", "勾选", "选择"], "筛选条件已正确设置"),
        (["播放", "开始"], "目标内容已开始播放"),
        (["下载", "保存"], "下载操作已完成"),
        (["登录", "注册"], "登录/注册操作已完成"),
        (["评论", "查看"], "评论区域可见且内容已加载"),
    ]
    for keywords, desc in keyword_map:
        if any(kw in task for kw in keywords):
            items.append({
                "id": f"chk_{idx:02d}",
                "description": desc,
                "check_method": "screenshot",
                "target_keywords": keywords,
                "checked": False,
                "result": None,
                "evidence": None,
            })
            idx += 1
    if not items:
        items.append({
            "id": "chk_01",
            "description": f"任务已完成：{task[:80]}",
            "check_method": "screenshot",
            "target_keywords": [],
            "checked": False,
            "result": None,
            "evidence": None,
        })
    logger.warning("[checklist fallback] generated %d items from keyword heuristics", len(items))
    return items


def _parse_checklist_json(raw: str) -> list | None:
    """Extract JSON array from LLM response, with Chinese quote repair."""
    # Try bare JSON first
    result = _safe_json_loads(raw)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]  # single object

    # Try depth-based extraction for outermost [...]
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == '[':
            if depth == 0:
                start = i
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start >= 0:
                result = _safe_json_loads(raw[start:i + 1])
                if isinstance(result, list):
                    return result
                start = -1

    # Fallback: greedy regex
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        result = _safe_json_loads(m.group(0))
        if isinstance(result, list):
            return result
    return None


def _safe_json_loads(text: str) -> object | None:
    """json.loads with Chinese quote repair and array-key repair."""
    import json as _json
    try:
        result = _json.loads(text)
        # C2: dict-as-array tolerance — if result is a dict with chk_0X keys,
        # extract .values() as the checklist array.
        if isinstance(result, dict) and any(k.startswith("chk_") for k in result):
            return list(result.values())
        return result
    except _json.JSONDecodeError:
        # Repair Chinese quotes first
        fixed = _repair_quotes(text)
        # Then repair stray "key": before { in arrays
        fixed = _repair_array_keys(fixed)
        if fixed != text:
            try:
                result = _json.loads(fixed)
                if isinstance(result, dict) and any(k.startswith("chk_") for k in result):
                    return list(result.values())
                return result
            except _json.JSONDecodeError:
                pass
    return None


def _repair_quotes(json_str: str) -> str:
    """Replace ASCII double-quotes used as Chinese punctuation inside string values."""
    result = []
    in_string = False
    escape_next = False
    quote_count = 0
    for i, ch in enumerate(json_str):
        if escape_next:
            escape_next = False
            result.append(ch)
            continue
        if ch == '\\':
            escape_next = True
            result.append(ch)
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                quote_count = 0
                result.append(ch)
            else:
                prev = result[-1] if result else ''
                ahead = json_str[i + 1] if i + 1 < len(json_str) else ''
                if ahead in (':', ',', '}', ']', '\n', '\r', ' ') or ahead == '':
                    in_string = False
                    result.append(ch)
                elif prev and _is_cjk(prev) and ahead and _is_cjk(ahead):
                    result.append('「' if quote_count % 2 == 0 else '」')
                    quote_count += 1
                else:
                    in_string = False
                    result.append(ch)
        else:
            result.append(ch)
    return ''.join(result)


def _repair_array_keys(json_str: str) -> str:
    """Remove stray \"key\": before { in JSON arrays, e.g. \"chk_02\": { → {.

    Only matches when the \"key\": follows a closing brace + comma (},),
    which indicates an array element boundary.  This avoids matching
    legitimate object properties like \"ui_check\": { inside objects.
    """
    import re as _re
    return _re.sub(r'(\},?\s*)"[^"]+"\s*:\s*(\{)', r'\1\2', json_str)


def _dump_parse_failure(raw: str) -> None:
    """Write the full raw text to a sidecar file for debugging parse failures."""
    import config as _cfg
    import time as _time
    from pathlib import Path
    try:
        owned_output = os.environ.get('PHONE_FACTORY_EVALUATION_OUTPUT')
        debug_dir = Path(owned_output) / 'debug' if owned_output else getattr(_cfg, "LOGS_DIR", None) or _cfg.PROJECT_ROOT / ".log"
        ts = _time.strftime("%Y%m%d_%H%M%S")
        path = debug_dir / f"checklist_parse_failure_{ts}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        logger.warning("[checklist parse] full raw (%s chars) saved to %s", len(raw), path)
    except Exception:
        pass


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or 0x3000 <= cp <= 0x303F or
            0xFF00 <= cp <= 0xFFEF or 0x2000 <= cp <= 0x206F or
            ch in '。，、；：！？）】》…—·～' or
            ch.isdigit() or ch.isalpha())
