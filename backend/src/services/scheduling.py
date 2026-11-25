"""整批保养单排程：在本月台班内为全部保养单寻找最早整体完工的排法。

约束（与业务口径一一对应）：
1. 每单连着做完、时长不变，只能排在所选月份的班次（台班）时间内；
2. 避开故障：计划/非计划停机占用的分钟不可排；
3. 同一时段只修一座月台（保养工单互斥，每分钟至多一个工单在做）；
4. 保养期间必须留一座在班且未停机的月台接货：
   维修某座月台时，其余月台里至少有一座在同一分钟处于「在班且无停机」状态。

整批排不下时返回全部冲突单号，不产出部分安排。
时间一律按仓库本地时间、自然月裁切，以分钟为最小粒度、半开区间处理。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic

from src.services.domain import (
    KINDS,
    MONTH,
    SHIFT,
    STAMP,
    Interval,
)

MONTH_MINUTES = 31 * 24 * 60
SEARCH_NODE_LIMIT = 200_000
SEARCH_DEADLINE_SECONDS = 3.0
# 超过该批量时直接采用贪心可行解（标注 exact=false），避免大整数位运算在大批量下放大延迟。
# 25 单以内走完整分支限界并给出可证明的最优解；超出则用保证可行的贪心解快速响应。
GREEDY_ONLY_LIMIT = 25


class SchedulingError(ValueError):
    """输入不满足排程前提（月份、班次或工单字段无效）。"""


@dataclass(frozen=True)
class Order:
    record_id: str
    dock: str
    minutes: int


@dataclass
class DockProfile:
    shift: int = 0          # 台班分钟位图（本月裁切后的并集）
    downtime: int = 0       # 计划+非计划停机分钟位图（本月裁切后的并集）
    working: int = 0        # 在班且未停机的分钟位图
    serviceable: int = 0    # 可排保养的分钟：在班、未停机，且另有月台可接货


def _month_period(month: str) -> Interval:
    if not isinstance(month, str) or not MONTH.fullmatch(month):
        raise SchedulingError('请按 YYYY-MM 填写有效排程月份，例如 2026-10。')
    try:
        start = datetime.fromisoformat(f'{month}-01')
    except ValueError as error:
        raise SchedulingError('请按 YYYY-MM 填写有效排程月份，例如 2026-10。') from error
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return Interval(start, end)


def _parse_stamp(text: str, line: int) -> datetime:
    if not isinstance(text, str) or not STAMP.fullmatch(text.strip()):
        raise SchedulingError(f'CSV 第 {line} 行时间无效，请按 YYYY-MM-DD HH:MM:SS 填写仓库当地时间。')
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError as error:
        raise SchedulingError(f'CSV 第 {line} 行时间无效，请按 YYYY-MM-DD HH:MM:SS 填写仓库当地时间。') from error


def _interval_bits(interval: Interval, period: Interval) -> tuple[int, int]:
    """把半开区间按整分钟栅格化，返回 (起始分钟偏移, 位图)。

    区间端点对齐到整分钟；落在本月之外的部分按自然月裁切，栅格不含结束分钟。
    """
    start = max(interval.start, period.start)
    end = min(interval.end, period.end)
    if start >= end:
        return 0, 0
    if (start.second or start.microsecond) and start > period.start:
        # 非整分钟起点向上取整，保证整段都在台班/停机区间内（半开、按秒语义）。
        start = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
    else:
        start = start.replace(second=0, microsecond=0)
    end = end.replace(second=0, microsecond=0)
    start = max(start, period.start)
    end = min(end, period.end)
    if start >= end:
        return 0, 0
    offset = int((start - period.start).total_seconds()) // 60
    span = int((end - start).total_seconds()) // 60
    if span <= 0:
        return 0, 0
    return offset, (1 << span) - 1 << offset


def build_profiles(rows: list[dict], period: Interval) -> dict[str, DockProfile]:
    """从合表记录构建各月台的台班/停机/可服务分钟位图。"""
    profiles: dict[str, DockProfile] = {}
    seen: dict[tuple[str, str, str], Interval] = {}
    for line, row in enumerate(rows, start=2):
        values = [row.get(key) for key in ('dock', 'kind', 'record_id', 'start', 'end')]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise SchedulingError(f'CSV 第 {line} 行字段缺失，请补齐月台、记录类型、记录编号、开始和结束时间。')
        dock, kind, record_id, start_text, end_text = (value.strip() for value in values)
        if kind not in KINDS:
            raise SchedulingError(f'CSV 第 {line} 行记录类型无效，请填写班次、计划停机或非计划停机。')
        start = _parse_stamp(start_text, line)
        end = _parse_stamp(end_text, line)
        if end <= start:
            raise SchedulingError(f'CSV 第 {line} 行请将结束时间改为晚于开始时间，跨夜记录请填写次日日期。')
        key = (dock, kind, record_id)
        interval = Interval(start, end)
        if key in seen and interval != seen[key]:
            raise SchedulingError(f'CSV 第 {line} 行记录编号 {record_id} 对应多个区间，请核对起止时间或更正编号。')
        seen[key] = interval
        profile = profiles.setdefault(dock, DockProfile())
        _offset, bits = _interval_bits(interval, period)
        if not bits:
            continue
        if kind == SHIFT:
            profile.shift |= bits
        else:
            profile.downtime |= bits

    if not any(profile.shift for profile in profiles.values()):
        raise SchedulingError('所选月份没有可用于排程的台班，请补齐覆盖该月的班次或改选月份。')

    docks = sorted(profiles)
    full_month = (1 << MONTH_MINUTES) - 1
    for dock, profile in profiles.items():
        profile.working = profile.shift & ~profile.downtime & full_month
    for dock in docks:
        profile = profiles[dock]
        others_working = 0
        for other in docks:
            if other != dock:
                others_working |= profiles[other].working
        # 维修本月台的每一分钟，都要有另一座月台在班且未停机。
        profile.serviceable = profile.working & others_working
    return profiles


def parse_orders(items: list[dict], profiles: dict[str, DockProfile]) -> list[Order]:
    """校验并解析保养单列表。"""
    orders: list[Order] = []
    seen_ids: set[str] = set()
    for line, item in enumerate(items, start=1):
        record_id = str(item.get('record_id', '')).strip()
        dock = str(item.get('dock', '')).strip()
        if not record_id or not dock:
            raise SchedulingError(f'第 {line} 张保养单缺少单号或月台编号。')
        if record_id in seen_ids:
            raise SchedulingError(f'保养单 {record_id} 重复提交，请每张单号只排一次。')
        try:
            minutes = int(item.get('minutes'))
        except (TypeError, ValueError) as error:
            raise SchedulingError(f'保养单 {record_id} 的时长需填写为正整数分钟。') from error
        if minutes <= 0:
            raise SchedulingError(f'保养单 {record_id} 的时长需填写为正整数分钟。')
        if dock not in profiles:
            raise SchedulingError(f'保养单 {record_id} 指定的月台 {dock} 在本月台账中不存在。')
        seen_ids.add(record_id)
        orders.append(Order(record_id, dock, minutes))
    if not orders:
        raise SchedulingError('请至少提交一张保养单。')
    return orders


def _runs_of_ones(bitmap: int):
    """枚举位图中连续为 1 的段，产出 (起始分钟, 长度)，按起点升序。"""
    while bitmap:
        low = bitmap & -bitmap
        start = low.bit_length() - 1
        block = bitmap >> start
        run = 0
        while block & 1:
            run += 1
            block >>= 1
        yield start, run
        bitmap &= ~(((1 << run) - 1) << start)


def _candidate_starts(serviceable: int, minutes: int) -> list[int]:
    """枚举能容纳指定时长的所有合法起点（按升序）。

    主要供独立穷举校验使用；主排程走 `_earliest_slot` 的位图移位以避免逐分钟枚举。
    """
    starts = []
    for run_start, run_length in _runs_of_ones(serviceable):
        if run_length >= minutes:
            starts.extend(range(run_start, run_start + run_length - minutes + 1))
    return starts


FULL_MONTH_MASK = (1 << MONTH_MINUTES) - 1


def _earliest_slot(serviceable: int, occupied: int, minutes: int) -> int | None:
    """在 serviceable 且未占用的分钟里，找长度为 minutes 的最早连续窗口，返回起点。

    利用位图移位求连续为 1 的长度，全程只做大整数运算，不枚举每个分钟。
    """
    free = serviceable & ~occupied & FULL_MONTH_MASK
    run = free
    # run 的某一位为 1，等价于从该位起连续 minutes 个分钟都可用。
    for _ in range(minutes - 1):
        run &= run >> 1
        if not run:
            return None
    if not run:
        return None
    return (run & -run).bit_length() - 1


def _minute_text(offset: int, period: Interval) -> str:
    return (period.start + timedelta(minutes=offset)).isoformat(sep=' ', timespec='seconds')


def schedule_maintenance(rows: list[dict], orders: list[dict], options: dict) -> dict:
    """计算整批保养单最早整体完工排程。

    成功返回 feasible=True 与逐单安排；整批排不下返回 feasible=False 与冲突单号，
    且不包含任何部分安排。单机排程的最优解可左对齐：每单都放在其当时最早可行
    窗口里，因此只需在「下一单放哪张」上分支，配合下界剪枝求最早整体完工。
    """
    month = options.get('month', '2026-10')
    period = _month_period(month)
    profiles = build_profiles(rows, period)
    parsed = parse_orders(orders, profiles)

    # 结构性冲突：该单在其月台上连单独排入的连续窗口都不存在。
    structural: list[str] = []
    for order in parsed:
        if _earliest_slot(profiles[order.dock].serviceable, 0, order.minutes) is None:
            structural.append(order.record_id)
    if structural:
        return _infeasible(month, parsed, period, profiles, structural,
                           '部分保养单在本月台的台班内找不到满足接货条件的连续窗口。', 'no_window')

    ordered = sorted(parsed, key=lambda item: item.record_id)
    by_id = {order.record_id: order for order in ordered}
    # 每张单在完全空台时的最早完工，作为固定的个体下界。
    solo_earliest_finish = {
        item.record_id: _earliest_slot(profiles[item.dock].serviceable, 0, item.minutes) + item.minutes
        for item in ordered
    }
    total_minutes = sum(item.minutes for item in ordered)

    def place(order: Order, occupied: int):
        start = _earliest_slot(profiles[order.dock].serviceable, occupied, order.minutes)
        if start is None:
            return None
        window = ((1 << order.minutes) - 1) << start
        return start, start + order.minutes, window

    def greedy_incumbent():
        """每步把能最早开工的单左对齐放入，给出可行的整体完工上界。"""
        occupied_now = 0
        placed: dict[str, int] = {}
        remaining_ids = {item.record_id for item in ordered}
        finish_max = 0
        while remaining_ids:
            best_choice = None
            for rid in remaining_ids:
                outcome = place(by_id[rid], occupied_now)
                if outcome is None:
                    continue
                start, end, _window = outcome
                choice = (start, rid, end, _window)
                if best_choice is None or (start, rid) < (best_choice[0], best_choice[1]):
                    best_choice = choice
            if best_choice is None:
                return None
            start, rid, end, window = best_choice
            placed[rid] = start
            remaining_ids.remove(rid)
            occupied_now |= window
            if end > finish_max:
                finish_max = end
        return finish_max, placed

    incumbent = greedy_incumbent()
    if incumbent is None:
        best = None
        best_finish = MONTH_MINUTES + 1
    else:
        best_finish, best = incumbent
        best = dict(best)

    # 根下界：
    # 1) 所有单背靠背的最早可达完工，并与每张单个体最早完工取最大；
    # 2) 每个月台在其可用窗口内装下该月台全部工单所需的最早时刻（按窗口前缀容量）。
    root_lower_bound = max([total_minutes, *solo_earliest_finish.values()])
    demand_by_dock = {dock: 0 for dock in profiles}
    for item in ordered:
        demand_by_dock[item.dock] += item.minutes
    for dock, need in demand_by_dock.items():
        if not need:
            continue
        bits = profiles[dock].serviceable
        # 找到前缀中可用分钟数 >= need 的最早分钟（二分定位）。
        lo, hi = 0, MONTH_MINUTES
        while lo < hi:
            mid = (lo + hi) // 2
            if (bits & ((1 << mid) - 1)).bit_count() >= need:
                hi = mid
            else:
                lo = mid + 1
        if lo > root_lower_bound:
            root_lower_bound = lo
    if best is not None and best_finish <= root_lower_bound:
        # 贪心解已达到可证明的下界，无需再做分支限界，直接判定为最优。
        return _build_plan_result(month, parsed, period, profiles, best, True)

    # 分支限界：节点表示一条部分左对齐序列；分支为下一张放哪张单。
    occupied = 0
    placement: dict[str, int] = {}
    remaining = {item.record_id for item in ordered}
    remaining_minutes = total_minutes
    placed_finish_max = 0
    nodes = 0
    budget_hits = False
    deadline = monotonic() + SEARCH_DEADLINE_SECONDS
    # 各月台剩余工单所需总时长，用于容量必要条件剪枝（月台数通常仅个位数）。
    remaining_by_dock = {dock: 0 for dock in profiles}
    for item in ordered:
        remaining_by_dock[item.dock] += item.minutes

    def lower_bound() -> int:
        # 已放最晚完工 + 剩余单背靠背（无空窗）总时长，再与各剩余单个体最早完工取最大。
        bound = placed_finish_max + remaining_minutes
        for rid in remaining:
            if solo_earliest_finish[rid] > bound:
                bound = solo_earliest_finish[rid]
        return bound

    def capacity_feasible() -> bool:
        # 必要条件：每个月台剩余单的总时长不得超过该月台当前可用分钟。
        free_bits = FULL_MONTH_MASK & ~occupied
        for dock, need in remaining_by_dock.items():
            if need and need > (profiles[dock].serviceable & free_bits).bit_count():
                return False
        return True

    def search() -> None:
        nonlocal best, best_finish, nodes, occupied, remaining_minutes, placed_finish_max, budget_hits
        if not remaining:
            if placed_finish_max < best_finish:
                best_finish = placed_finish_max
                best = dict(placement)
            return
        nodes += 1
        if nodes > SEARCH_NODE_LIMIT or monotonic() > deadline:
            budget_hits = True
            return
        if not capacity_feasible():
            return
        if lower_bound() >= best_finish:
            return
        # 先算每个剩余单的最早落点，按开工时刻升序分支（并列按单号，保证可复现）。
        choices = []
        for rid in sorted(remaining):
            outcome = place(by_id[rid], occupied)
            if outcome is not None:
                start, end, window = outcome
                choices.append((start, rid, end, window))
        choices.sort(key=lambda choice: (choice[0], choice[1]))
        for start, rid, end, window in choices:
            order = by_id[rid]
            placement[rid] = start
            remaining.remove(rid)
            occupied |= window
            remaining_minutes -= order.minutes
            remaining_by_dock[order.dock] -= order.minutes
            previous_max = placed_finish_max
            if end > placed_finish_max:
                placed_finish_max = end
            search()
            placed_finish_max = previous_max
            remaining_by_dock[order.dock] += order.minutes
            remaining_minutes += order.minutes
            occupied &= ~window
            remaining.add(rid)
            del placement[rid]

    if len(ordered) <= GREEDY_ONLY_LIMIT:
        search()
        exact = not budget_hits
    else:
        # 大批量：贪心解已保证可行且通常接近最优，但不做全局最优证明。
        exact = False

    if best is None:
        # 用与搜索无关的容量必要条件做独立判定，避免在未证伪时误报“排不下”。
        demand_by_dock = {dock: 0 for dock in profiles}
        for item in parsed:
            demand_by_dock[item.dock] += item.minutes
        overloaded = sorted(
            item.record_id for item in parsed
            if demand_by_dock[item.dock] > profiles[item.dock].serviceable.bit_count()
        )
        if overloaded:
            return _infeasible(month, parsed, period, profiles, overloaded,
                               '该月台全月可排保养分钟不足以容纳分配给它的保养单总时长。', 'dock_capacity')
        if budget_hits:
            raise SchedulingError('排程搜索在限时内未能收敛，请缩小保养单批量或减少长时段工单后重试。')
        conflict_ids = sorted(order.record_id for order in parsed)
        return _infeasible(month, parsed, period, profiles, conflict_ids,
                           '台班窗口无法容纳整批保养单（同一时段只修一座且须保留接货月台）。', 'batch_capacity')

    return _build_plan_result(month, parsed, period, profiles, best, exact)


def _build_plan_result(month, parsed, period, profiles, best, exact):
    schedule_rows = []
    for order in sorted(parsed, key=lambda item: (best[item.record_id], item.record_id)):
        start = best[order.record_id]
        schedule_rows.append({
            '保养单号': order.record_id,
            '月台': order.dock,
            '时长（分钟）': order.minutes,
            '开始（含）': _minute_text(start, period),
            '结束（不含）': _minute_text(start + order.minutes, period),
            '完工分钟偏移': start + order.minutes,
        })
    finish_offset = max(row['完工分钟偏移'] for row in schedule_rows)
    notes = _notes(month, profiles)
    if exact:
        notes.append('已通过下界剪枝确认，该排法的整体完工时间为全月最早（最优解）。')
    else:
        notes.append('批量较大或触达搜索时限，返回的是保证可行的最早可得贪心结果，未做全局最优证明。')
    return {
        'feasible': True,
        'month': month,
        'status': 'scheduled',
        'exact': exact,
        'headline': f'{month} 整批 {len(parsed)} 张保养单可全部完工，最早整体完工时间 {_minute_text(finish_offset, period)}。',
        'metrics': {
            '保养单数': len(parsed),
            '涉及月台数': len({order.dock for order in parsed}),
            '保养总时长（分钟）': sum(order.minutes for order in parsed),
            '最早整体完工时间': _minute_text(finish_offset, period),
            '冲突单数': 0,
        },
        'rows': schedule_rows,
        'conflicts': [],
        'notes': notes,
    }


def _infeasible(month: str, orders: list[Order], period: Interval,
                profiles: dict[str, DockProfile], conflict_ids: list[str], reason: str, status: str) -> dict:
    conflict_set = set(conflict_ids)
    return {
        'feasible': False,
        'month': month,
        'status': status,
        'headline': f'{month} 整批 {len(orders)} 张保养单排不下，冲突 {len(conflict_ids)} 张，未保存任何部分安排。',
        'metrics': {
            '保养单数': len(orders),
            '涉及月台数': len({order.dock for order in orders}),
            '保养总时长（分钟）': sum(order.minutes for order in orders),
            '最早整体完工时间': None,
            '冲突单数': len(conflict_ids),
        },
        'rows': [],
        'conflicts': [
            {
                '保养单号': order.record_id,
                '月台': order.dock,
                '时长（分钟）': order.minutes,
                '冲突原因': reason,
            }
            for order in sorted((item for item in orders if item.record_id in conflict_set),
                                key=lambda item: item.record_id)
        ],
        'notes': _notes(month, profiles),
    }


def _notes(month: str, profiles: dict[str, DockProfile]) -> list[str]:
    notes = [
        f'排程仅使用 {month} 自然月内的台班分钟，跨月班次与停机按自然月裁切，区间含起点、不含终点，最小粒度 1 分钟。',
        '每张保养单连续完成、时长不变；保养时段必须落在本班次内并避开计划与非计划停机。',
        '同一分钟至多安排一张保养单；维修某座月台时，其余月台至少有一座在班且未停机以承接到货。',
        '整批排不下时仅返回冲突单号，不保存任何部分安排；同一数据版本与同一批工单重复提交得到同一版本结果。',
    ]
    for dock in sorted(profiles):
        profile = profiles[dock]
        notes.append(
            f'{dock}：台班 {profile.shift.bit_count()} 分钟，停机 {profile.downtime.bit_count()} 分钟，'
            f'可排保养窗口 {profile.serviceable.bit_count()} 分钟。'
        )
    return notes
