"""Prompts for embodied scenario / task synthesis.

The prompts embed a distilled version of the frozen v0.1 specs
(data/scenarios/README.md, data/tasks/README.md) plus few-shot exemplars
loaded from data/. They are the contract the LLM must satisfy; the
programmatic gates (ows/gen/validate.py + ows env compile) enforce it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCENARIO_EXAMPLE_PATH = _REPO_ROOT / "data" / "scenarios" / "home_01.json"
_TASK_EXAMPLE_DIRECT = _REPO_ROOT / "data" / "tasks" / "home" / "home_01_umbrella_move.json"
_TASK_EXAMPLE_COMPOSITE = _REPO_ROOT / "data" / "tasks" / "market" / "market_01_grocery_run.json"


def _read_json(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Scenario generation
# ---------------------------------------------------------------------------

SCENARIO_SYSTEM_PROMPT = """你是一个具身智能场景设计师。根据用户给出的主题，生成一个全新的、符合 OpenWorldSandbox 场景包规范 v0.1 的场景 JSON。
只输出 JSON 对象本身，不要输出 markdown 代码块标记、解释或任何前后缀文字。

## 顶层结构
{
  "scenario_id": "<snake_case 英文 id，如 office_01>",
  "spec_version": "0.1",
  "name": "<场景中文名>",
  "description": "<可选，场景描述>",
  "areas": [{"id": "<snake_case>", "name": "<中文名>"}],
  "area_adjacency": [{"from": "<area_id>", "to": "<area_id>", "passable": true}],
  "area_tables": {"<area_id>": [<Entity>...]},
  "robot": {"location": "<area_id>", "left_hand": null, "right_hand": null}
}

## 区域与连通规则
- areas：4–7 个区域
- area_adjacency 是无向图，passable:true 的边构成全连通图（任意两区域可达）
- from/to 必须引用 areas 中存在的 id

## Entity 字段
- id：全局唯一，格式 <class>_<NN>（如 sofa_01、cup_01）
- class：furniture | container | device | clothing | item | consumable | tool | fixture
- name：中文名
- pickable：bool。家具/固定设施/设备一般 false；衣物/物品/工具/消耗品一般 true；可整体搬运的容器 true（且 properties 加 portable）
- on / in：互斥。on 的目标必须 properties 含 can_support（表面）；in 的目标必须含 can_contain（容器）；引用链不能成环
- is_device：设备类实体为 true
- open_state："open" | "closed"，只给可开合容器/设备；不填视为常开（内部始终可见）
- device_state："off" | "running"
- properties 枚举（方向约定，不可混用）：
  接收方能力（can_*）：can_support、can_contain、can_hang、can_wash、hangable_inside、has_water、transactional
  物品自身特性（*able）：portable、absorbent、soft、waterproof、hangable、washable
  典型组合：表面=can_support；容器=can_contain；衣筐=portable+can_contain；洗衣机=can_contain+can_support+can_wash；水槽=can_contain+can_support+has_water；晾衣架=can_support+can_hang；交易类设备（收银台/POS）额外加 transactional（启动即记交易完成标记，之后关机不回退）
- states：键值必须为 string/number/boolean/null（不能嵌套对象）。常用键：cleanliness(dirty/clean)、condition(intact/damaged/used)、amount(full/empty)、on_off、temperature、locked 等，可按主题自创

## 高频错误提醒（compile 的 S 规则会拒绝，逐条自查）
- on 的目标必须 properties 含 can_support。设备、容器本身若无 can_support 就不能放东西：比如想在碎纸机/复印机上放文件，必须给该实体加 "can_support"（或把物品放旁边桌面上）
- in 的目标必须含 can_contain，且不能成环（A 在 B 里、B 又在 A 里）
- area_adjacency 必须全连通：每个区域至少通过一条 passable 边连入整体
- robot 的 left_hand/right_hand 只能为 null，不要预置持有物品

## 场景质量要求
- 实体总数 15–35
- 至少 2 个 open_state 为 closed 的容器（考察容器开合）
- 至少 1 个设备（考察设备操作）
- 部分实体放在容器内或表面上（考察搜索与取放链）
- 部分实体带 states（考察状态感知任务）
- 至少有 1 个拾取物 + 1 个可开合容器可支撑典型任务链
- 实体与区域命名贴近主题，中文 name 自然"""

SCENARIO_USER_TEMPLATE = """主题：{theme}
请设计一个全新的 {theme} 场景（不要复制示例，实体和区域要按主题重新设计）。

以下是一个符合规范的家庭场景完整示例，仅作格式参考：

{example}

请直接输出你的场景 JSON。"""


def build_scenario_messages(theme: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SCENARIO_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": SCENARIO_USER_TEMPLATE.format(
                theme=theme, example=_read_json(_SCENARIO_EXAMPLE_PATH)
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Task generation
# ---------------------------------------------------------------------------

TASK_SYSTEM_PROMPT = """你是一个具身智能任务出题人。根据给定的场景 JSON，生成一个符合 OpenWorldSandbox 任务包规范 v0.1 的任务 JSON。
只输出 JSON 对象本身，不要输出 markdown 代码块标记、解释或任何前后缀文字。

## 顶层字段
{
  "task_id": "<snake_case，建议 <scenario_id>_<任务名>，如 office_01_move_umbrella>",
  "spec_version": "0.1",
  "scenario_id": "<必须等于给定场景的 scenario_id>",
  "name": "<中文任务名>",
  "instruction": "<中文自然语言指令，明确告诉机器人要达成什么>",
  "task_type": "direct（单一目标链） | composite（多子目标）",
  "capability_tags": [从下面 10 个标签中选 2–4 个],
  "max_steps": "<正整数，必须严格大于最长 walkthrough 的动作数>",
  "initial_state_patch": {可选扰动，见下},
  "goal": {GoalCondition},
  "subgoals": [可选],
  "walkthroughs": [至少 1 条]
}

## capability_tags 枚举
navigation、pick_and_place、container_open_close、device_operation、multi_step、tool_use、state_awareness、hand_management、search、error_recovery

## Goal DSL（键名严格固定，禁止使用 entity_id/type/attr 等别名）
- eq：{"entity": "<id>", "field": "<field>", "op": "eq", "value": <值>}
- in：{"entity": "<id>", "field": "<field>", "op": "in", "value": [<值>...]}
- all_of / any_of：{"all_of": [<条件>...]} 或 {"any_of": [...]}
- count：{"op": "count", "entity_class": "<class>", "where": {"field": "<field>", "op": "eq", "value": <值>}, "cmp": "eq|neq|gt|gte|lt|lte", "value": <整数>}
叶子条件必须是 {"entity", "field", "op", "value"} 四个键；entity 与 field 缺一不可（entity 用实体 id 或 "robot"）。
示例（把伞放进衣柜）：
{"all_of": [{"entity": "umbrella_01", "field": "container_id", "op": "eq", "value": "wardrobe_01"}]}
field 支持：
  container_id — 实体所在容器 id（仅 in 关系；放在表面上不算——防止"放洗衣机顶上"冒充"放进洗衣机"）
  on — 实体所在表面 id
  area_id — 实体所在区域（沿容器/表面链上溯到区域）
  held_by — "left_hand" | "right_hand" | null
  open_state / device_state / states.<key>
entity 可用 "robot" 伪实体（field 支持 location、left_hand、right_hand）

## 非平凡性（硬性要求，compile 会拒绝违反者）
初始状态下 goal 必须不成立，任务必须真的需要行动才能完成。
出题前先在脑中核对：初始场景里目标条件是否为假？若已为真，换个目标或先用 initial_state_patch 扰动初始状态。

## Subgoal（可选，composite 任务建议 2–4 个）
- 每个动作执行后求值一次，一旦满足即锁存（后续状态变化不撤销）
- 条件要描述过程中某一时刻可观测的状态；不要用 all_of 要求多个瞬态同时成立（如两件衣物同时被双手持有——机器人只有两只手、逐件搬运，永远不可能同时成立），应拆成每件一个子目标

## Walkthrough（必须；compile 会逐步回放，任何一步失败则整个任务被拒）
- 动作名只能是 17 个之一：
  observe_scene、search_object、inspect_entity、check_robot_state、move_to、pick_object、place_object、open_container、close_container、hang_object、start_device、stop_device、apply_physical_tool、finish_task、report_target_absent、report_unable_to_continue、abandon_task
- 参数格式：
  move_to{"area_id"}、pick_object{"entity_id"}、place_object{"entity_id","target_id"}、open_container{"entity_id"}、close_container{"entity_id"}、hang_object{"entity_id","target_id"}、start_device{"entity_id"}、stop_device{"entity_id"}、apply_physical_tool{"tool_id","target_id","intended_effect"?}、search_object{"entity_id"}、inspect_entity{"entity_id"}、observe_scene{}、check_robot_state{}、finish_task{}、report_target_absent{"entity_id"}、report_unable_to_continue{"reason"}、abandon_task{}
- 回放规则（每步必须 success）：
  * pick 前提：实体 pickable=true、与机器人同区域（或在该区域开放容器/表面上）、不在闭合容器深处、机器人有空手
  * place 前提：实体在手里；目标是当前区域的容器（can_contain 且 open_state 非 closed）或表面（can_support）；不能放回自己/自己的容器链内
  * 放进 closed 容器前必须先 open_container；要进新区域必须 move_to 且该区域与当前区域邻接且 passable
  * 双手最多同时持有 2 件，第三件要先放下一件
  * 洗衣机等容器类设备：启动前必须 closed（先放衣物→关门→start_device）
  * 每步动作只使用场景中真实存在的实体 id / 区域 id
- walkthrough 步数必须 < max_steps；末尾以 finish_task 结束（可选但建议）
- 可写 2 条不同方案（不同路径/手法），每条都必须可成功回放

## initial_state_patch（可选，制造扰动用）
{"entities": {"<id>": {"states": {...} / "open_state": ...}}, "robot": {...}, "area_adjacency": [{"from": .., "to": .., "passable": false}]}
- 未列出的保持场景默认；states 整体替换；entity id 必须存在于场景
- 不要写与场景默认值相同的冗余 patch（会被警告）"""

EXPLORATION_TASK_RULES = """

## 探索型任务附加要求
- instruction 必须像真实用户说话，明确用户意图，但不能写实体 id、确切藏匿位置或完整操作步骤。
- 任务必须包含一个“关键未知事实”，例如目标物位置、候选物状态、设备运行状态或库存状态。
- Robot 必须能在关键操作前通过 observe_scene、inspect_entity 或打开容器获得该事实；不能依赖猜测。
- 不同的关键事实必须会改变目标对象或高效行动路线，否则不算探索任务。
- walkthrough 在首次关键操作前应包含必要的信息获取动作，但不得故意执行失败动作来制造“试错”。
- observe_scene 已返回可见实体的完整状态，不要无意义地强制 inspect_entity。
- search_object 需要预先知道 entity_id，不能把它当成通用搜索工具。
- goal 只描述最终成功状态，不得硬编码操作路线；subgoal 不得声称“已经观察”或“已经检查”。
- initial_state_patch 只能修改场景中已有实体和状态，禁止新增实体、动作、字段类型或 Goal DSL 操作符。
- initial_state_patch 禁止把 in/on 显式改为 null（当前引擎不支持，会直接报错）；环境差异用 states 扰动、open_state/device_state 或 area_adjacency 表达。
- max_steps 必须 ≥ 最短 walkthrough 动作数 × 1.8 + 4（例如 walkthrough 10 步 → max_steps ≥ 22），给探索留足余量。
- 如果用户要求生成某个任务的环境变体，保持自然语言意图基本相同，但改变关键未知事实，使正确对象或高效路线发生变化；两个版本的 goal 目标实体/状态必须确实不同，防止记住固定实体 ID 或固定路线同时通过两个版本。
- 输出前自检：初始 goal 为 false、walkthrough 可执行、最终 goal 为 true、指令与 goal 含义一致。
"""

TASK_USER_TEMPLATE = """场景 JSON（任务必须严格引用其中真实存在的实体 id 与区域 id）：

{scenario}

参考示例 1（direct 任务，家庭场景）：

{example_direct}

参考示例 2（composite 任务，超市场景）：

{example_composite}

出题要求：{requirement}

请直接输出 1 个新任务 JSON。"""


def build_task_messages(
    scenario: dict[str, Any] | str,
    requirement: str,
    *,
    exploration: bool = False,
) -> list[dict[str, str]]:
    scenario_text = (
        scenario
        if isinstance(scenario, str)
        else json.dumps(scenario, ensure_ascii=False, indent=2)
    )
    return [
        {
            "role": "system",
            "content": (
                TASK_SYSTEM_PROMPT + EXPLORATION_TASK_RULES
                if exploration
                else TASK_SYSTEM_PROMPT
            ),
        },
        {
            "role": "user",
            "content": TASK_USER_TEMPLATE.format(
                scenario=scenario_text,
                example_direct=_read_json(_TASK_EXAMPLE_DIRECT),
                example_composite=_read_json(_TASK_EXAMPLE_COMPOSITE),
                requirement=requirement,
            ),
        },
    ]


def build_exploration_task_messages(
    scenario: dict[str, Any] | str,
    requirement: str,
) -> list[dict[str, str]]:
    """Build task-generation messages with exploration-specific constraints."""
    return build_task_messages(scenario, requirement, exploration=True)


# ---------------------------------------------------------------------------
# Default themes / task requirements (diversity guidance)
# ---------------------------------------------------------------------------

DEFAULT_THEMES_SMOKE = ["办公室"]
DEFAULT_THEMES_FULL = ["办公室", "餐厅", "诊所", "车库", "咖啡店"]

DEFAULT_TASK_REQUIREMENTS = [
    "direct 任务，侧重 navigation + pick_and_place，简单取放链",
    "composite 任务，侧重 container_open_close + search，多子目标",
    "direct 任务，侧重 device_operation，含设备开关",
    "composite 任务，侧重 state_awareness + tool_use（清洁/修复类），含 initial_state_patch 扰动",
    "direct 任务，侧重 hand_management，需要搬运多件物品",
]


def pick_requirements(n: int) -> list[str]:
    """Round-robin the default requirements up to n items."""
    reqs = DEFAULT_TASK_REQUIREMENTS
    return [reqs[i % len(reqs)] for i in range(n)]


# ---------------------------------------------------------------------------
# Exploration task families (ARC-AGI-3 style, plan §3)
# ---------------------------------------------------------------------------

EXPLORATION_FAMILIES: list[dict[str, str]] = [
    {
        "family": "find_thing",
        "scenario_id": "home_01",
        "task_id_base": "home_01_laundry_supply",
        "requirement": (
            "找东西类探索任务。用户意图：洗衣液不在平时的位置，请机器人找到它并放到洗衣机旁。\n"
            "- instruction 必须像真实用户说话（参考说法：「洗衣液不在平时的位置，帮我找出来放到洗衣机旁」），"
            "不得包含实体 id、藏匿位置或操作步骤。\n"
            "- 关键未知事实＝洗衣液的位置，必须通过观察或打开容器才能确定。\n"
            "- 设计一个初始状态让洗衣液不在洗衣机旁（用 initial_state_patch 把洗衣液移入某个闭合容器），"
            "goal 只约束终态：洗衣液位于阳台上的某个合法表面（on 某 can_support 实体，如晾衣架），"
            "不要额外约束指令未提及的实体。\n"
            "- 【重要】不要把洗衣机本体作为 place 目标：它有 can_contain，会被当作需先开门的容器；"
            "目标表面选晾衣架等其他 can_support 实体。\n"
            "- 两个环境变体必须把洗衣液藏在【不同】的容器或区域，使记住固定藏匿路线的策略"
            "无法同时通过两个版本。\n"
            "- 不得新增实体或动作，只用 home_01 已有实体。"
        ),
    },
    {
        "family": "pick_candidate",
        "scenario_id": "market_01",
        "task_id_base": "market_01_pick_good_apple",
        "requirement": (
            "从候选物中选择类探索任务。用户意图：货架上有两个苹果，其中一个磕坏了，请挑一个好的装袋结账。\n"
            "- instruction 必须像真实用户说话（参考说法：「这两个苹果里有一个磕坏了，帮我挑个好的装袋结账」），"
            "不得包含实体 id 或哪个是好苹果。\n"
            "- 关键未知事实＝哪个苹果是好的，必须通过 inspect_entity（或等价观察）确认状态（如 states.condition）。\n"
            "- 用 initial_state_patch 设置两个苹果的不同 condition；goal 只约束终态："
            "好苹果被放进购物袋（并完成结账），且坏苹果【不得】在购物袋中（须留在原处）——"
            "防止把两只都装袋的固定策略。\n"
            "- 两个环境变体必须是【不同】的苹果为好果（v1 好果是 A、v2 好果是 B），"
            "使记住固定实体 ID 的策略无法同时通过两个版本。\n"
            "- 不得新增实体或动作，只用 market_01 已有实体（若场景没有苹果，选两个同类同状态可区分的候选物）。"
        ),
    },
    {
        "family": "judge_state",
        "scenario_id": "home_01",
        "task_id_base": "home_01_living_room_shutdown",
        "requirement": (
            "判断当前状态类探索任务。用户意图：准备睡了，请把客厅里还开着的电器关掉。\n"
            "- instruction 必须像真实用户说话（参考说法：「我准备睡了，把客厅里还开着的电器关掉」），"
            "不得指定具体哪台设备是开的。\n"
            "- 关键未知事实＝客厅里哪台设备正在运行，必须通过观察确定，不能固定操作某一台设备。\n"
            "- 用 initial_state_patch 将客厅中【恰好一台】设备设为 running（其余设备 off）；"
            "goal 约束【该台初始 running 的设备】最终 device_state 为 off——"
            "两个环境变体必须选不同的设备作为运行设备，因此 v1/v2 的 goal 目标实体不同，"
            "记住固定实体 ID 的机器人会在其中一个版本失败。\n"
            "- 不得新增实体或动作，只用 home_01 客厅已有实体。"
        ),
    },
    {
        "family": "find_problem",
        "scenario_id": "market_01",
        "task_id_base": "market_01_silent_restock",
        "requirement": (
            "发现问题并处理类探索任务。用户意图：顾客说冷藏柜里没牛奶了，请确认情况并处理好。\n"
            "- instruction 必须像真实用户说话（参考说法：「顾客说冷藏柜里没牛奶了，你确认一下并处理好」），"
            "不得包含库存位置或补货步骤。\n"
            "- 关键未知事实＝冷藏柜是否真的缺货、库存牛奶在哪里，必须通过观察/搜索确认。\n"
            "- 用 initial_state_patch 制造缺货：把牛奶移入另一个容器（\"in\": \"<另一容器 id>\"）表达"
            "冷藏柜无货，禁止把 in/on 置为 null；并安排备用库存位置。\n"
            "- goal 只约束终态：冷藏柜里有牛奶可售（数量或位置满足补货结果），"
            "不要额外约束指令未提及的实体。\n"
            "- 两个环境变体必须使【同一固定实体选择】在其中一个版本不可行：例如用 "
            "area_adjacency patch 在其中一个版本阻断通往仓库的通道（passable: false），"
            "迫使另一个版本改用另一盒奶/另一位置；或把两盒奶分别放入两个版本中位置不同的容器，"
            "且至少一盒在某一版本不可达。禁止只调换名称但两条固定路线都永远可行。\n"
            "- 不得新增实体或动作，只用 market_01 已有实体。"
        ),
    },
]

EXPLORATION_V2_REQUIREMENT_TEMPLATE = """【环境变体 v2】
为上述任务生成环境变体：用户意图与 v1 基本相同，但关键未知事实必须不同（正确对象或高效路线随之变化）。
v1 参考（仅用于对齐意图，不得照抄 v1 的事实与实体选择）：
- v1 指令：{v1_instruction}
- v1 goal：{v1_goal}
- v1 initial_state_patch：{v1_patch}
请直接输出 1 个新任务 JSON。"""


def build_v2_requirement(base_requirement: str, v1: dict) -> str:
    """Family requirement + v1 context for variant generation (plan §8)."""
    return base_requirement + "\n" + EXPLORATION_V2_REQUIREMENT_TEMPLATE.format(
        v1_instruction=v1.get("instruction", ""),
        v1_goal=json.dumps(v1.get("goal", {}), ensure_ascii=False),
        v1_patch=json.dumps(v1.get("initial_state_patch", {}), ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Exploration task structured review (plan §5.1)
# ---------------------------------------------------------------------------

EXPLORATION_REVIEW_SYSTEM_PROMPT = """你是具身任务数据质量审查员。判断给定的探索型任务是否真的需要 Robot 先观察环境、再行动。
你会同时看到该任务的【配对环境变体】（v1/v2 指令基本相同但环境状态不同）。判定的核心标准是：
**记住固定实体 ID 或固定行动路线的策略，必须无法同时通过两个版本**——
单看一个版本时固定路线可能侥幸通过，但跨版本必然失败，才算真的需要观察。
只输出一个 JSON 对象，键固定为：
{
  "pass": true 或 false,
  "key_fact": "<任务要求 Robot 发现的关键未知事实是什么>",
  "observation_action": "<通过什么合法动作（observe_scene/inspect_entity/search_object/open_container 等）可以获得该事实>",
  "decision_change": "<获得事实后，哪个决策（目标对象或高效路线）会发生变化>",
  "fixed_route_risk": "<同一固定实体 ID / 固定路线是否能同时通过两个版本？能则说明原因>",
  "instruction_goal_consistent": true 或 false,
  "reasons": "<通过或不通过的具体理由，中文>"
}
判定标准：
- instruction 不得泄露实体 ID、确切位置或完整操作步骤；
- 必须存在可通过合法动作获得的关键事实；考虑配对变体后，该事实必须改变正确对象或高效路线；
- walkthrough 在关键操作前包含信息获取动作；
- goal 只判断最终状态，与 instruction 含义一致（不要额外约束指令未提及的实体）；
- 若同一固定策略能同时通过两个版本 → pass=false。
不通过时 reasons 必须写明具体违反哪一条。"""


def build_review_messages(
    scenario: dict, task: dict, paired_task: dict | None = None
) -> list[dict[str, str]]:
    """GPT-5 structured review input: scenario brief + task JSON + paired
    variant context (plan §5.1, fixed_route_risk 需要跨版本判断)."""
    entities = []
    for area_id, ents in (scenario.get("area_tables") or {}).items():
        for e in ents:
            entities.append(
                {"id": e.get("id"), "class": e.get("class"), "name": e.get("name")}
            )
    scenario_brief = {
        "scenario_id": scenario.get("scenario_id"),
        "areas": [a.get("id") for a in scenario.get("areas", [])],
        "entities": entities,
    }
    task_brief = {
        "task_id": task.get("task_id"),
        "instruction": task.get("instruction"),
        "task_type": task.get("task_type"),
        "capability_tags": task.get("capability_tags"),
        "goal": task.get("goal"),
        "subgoals": task.get("subgoals"),
        "initial_state_patch": task.get("initial_state_patch"),
        "walkthroughs": task.get("walkthroughs"),
        "max_steps": task.get("max_steps"),
    }
    paired_text = ""
    if paired_task is not None:
        paired_brief = {
            k: paired_task.get(k)
            for k in ("task_id", "instruction", "goal", "initial_state_patch",
                      "walkthroughs", "max_steps")
        }
        paired_text = (
            "\n\n配对环境变体（同一意图的另一版本，用于判断固定路线风险）：\n"
            + json.dumps(paired_brief, ensure_ascii=False, indent=2)
        )
    return [
        {"role": "system", "content": EXPLORATION_REVIEW_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "场景摘要：\n"
            + json.dumps(scenario_brief, ensure_ascii=False, indent=2)
            + "\n\n候选任务：\n"
            + json.dumps(task_brief, ensure_ascii=False, indent=2)
            + paired_text
            + "\n\n请输出审查 JSON。",
        },
    ]
