"""恰好覆盖（exact cover）与总量一致性判定。

输入是单个分量内、同一轮次、未被撤销的有效票据。每张票覆盖一个明确标签
集合，并带有只对整票有效的非负整数件数。

判定思路
--------
1. 用位掩码表示标签集合，问题等价于：能否选出若干张票，使票的掩码两两不相交
   且并集恰好等于分量全部标签（exact cover）。
2. 记忆化搜索按“剩余标签掩码”定义状态，状态空间至多 ``2**n``（n <= 18），
   因此不会随票据数量指数爆炸；每张票在同一状态下至多被消费一次，
   最坏操作量约为 ``票数 * 2**n``，在 100 票 / 18 标签的约定上限内可控。
3. 每个状态记录：是否有完整覆盖、覆盖数（封顶计数）、总量是否在所有完整
   覆盖中一致、一套证据票组；一旦发现同状态可由不同路径得到不同总量，
   立刻保留两套互相冲突的完整覆盖证据并标记歧义，而不是继续枚举全部组合。
4. 不在分量之间做组合乘法：每个分量独立给出结论，库位层只汇总结论与件数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .models import CoverEvidence, Status
from .models import Ticket as TicketModel

# 覆盖数只用于可观测性与测试，封顶以免极端情况下产生超大整数。
_COVER_COUNT_CAP = 1_000_000


@dataclass(frozen=True)
class MaskedTicket:
    ticket_id: str
    mask: int
    quantity: int


@dataclass
class _State:
    """单个剩余掩码状态的聚合结论。

    总量不单独沿状态传递：证据票组确定后按票 ID 重新求和，
    避免歧义证据向上冒泡时总量与票组脱节。
    """

    status: Status
    total: Optional[int]
    evidence: Tuple[str, ...]
    second_evidence: Optional[Tuple[str, ...]]
    cover_count: int


def analyze_component(
    labels: Sequence[str],
    tickets: Sequence[TicketModel],
) -> Tuple[Status, Optional[int], Optional[CoverEvidence], Optional[CoverEvidence],
           Optional[int], Tuple[str, ...]]:
    """分析单个分量。

    返回 ``(状态, 精确总量, 证据票组, 冲突时的第二套证据, 覆盖数, 障碍说明)``。
    """

    index = {label: i for i, label in enumerate(labels)}
    full_mask = (1 << len(labels)) - 1
    masked: List[MaskedTicket] = []
    for t in tickets:
        mask = 0
        for label in t.labels:
            mask |= 1 << index[label]
        masked.append(MaskedTicket(t.ticket_id, mask, t.quantity))

    memo: Dict[int, _State] = {}

    def solve(remaining: int) -> _State:
        if remaining in memo:
            return memo[remaining]
        if remaining == 0:
            state = _State(Status.ACCEPTED, 0, (), None, 1)
            memo[remaining] = state
            return state

        # MRV：优先处理可选票据最少的标签，尽早收敛，也便于输出可复查障碍。
        pivot = _pick_pivot(remaining, masked)
        candidates = [
            mt for mt in masked
            if (mt.mask & (1 << pivot)) and (mt.mask & ~remaining) == 0
        ]

        best: Optional[_State] = None
        for mt in candidates:
            sub = solve(remaining ^ mt.mask)  # mt.mask 是 remaining 的非空真子集
            if sub.status == Status.NO_COVER:
                continue

            if sub.status == Status.AMBIGUOUS:
                # 子状态已经歧义，把当前票接到两套冲突证据前面直接上抛。
                state = _State(
                    Status.AMBIGUOUS,
                    None,
                    (mt.ticket_id,) + sub.evidence,
                    (mt.ticket_id,) + sub.second_evidence
                    if sub.second_evidence is not None else None,
                    min(_COVER_COUNT_CAP, sub.cover_count),
                )
                memo[remaining] = state
                return state

            # 子状态接受：当前票组总量 = 整票件数 + 子覆盖总量（绝不摊分）。
            total = mt.quantity + (sub.total or 0)
            evidence = (mt.ticket_id,) + sub.evidence
            count = min(_COVER_COUNT_CAP, sub.cover_count)
            if best is None:
                best = _State(Status.ACCEPTED, total, evidence, None, count)
            else:
                merged_count = min(_COVER_COUNT_CAP, best.cover_count + count)
                if best.status == Status.AMBIGUOUS:
                    best.cover_count = merged_count
                elif total != best.total:
                    # 同一剩余标签存在两套完整覆盖但总量不一致：歧义成立。
                    best = _State(
                        Status.AMBIGUOUS,
                        None,
                        best.evidence,
                        evidence,
                        merged_count,
                    )
                else:
                    best = _State(
                        Status.ACCEPTED,
                        best.total,
                        best.evidence,
                        None,
                        merged_count,
                    )

        if best is None:
            best = _State(Status.NO_COVER, None, (), None, 0)
        memo[remaining] = best
        return best

    result = solve(full_mask)

    evidence = (
        CoverEvidence(tuple(result.evidence), result.total)
        if result.status == Status.ACCEPTED else None
    )
    second = None
    if result.status == Status.AMBIGUOUS:
        evidence = CoverEvidence(tuple(result.evidence), _total_of(masked, result.evidence))
        assert result.second_evidence is not None
        # 总量始终按证据票组实际重算，保证“票组 ↔ 数量”严格一致、可复查。
        second = CoverEvidence(
            tuple(result.second_evidence),
            _total_of(masked, result.second_evidence),
        )

    obstacles: Tuple[str, ...] = ()
    if result.status == Status.NO_COVER:
        obstacles = _diagnose(full_mask, masked, labels, memo)

    cover_count = result.cover_count if result.status == Status.ACCEPTED else None
    return result.status, result.total, evidence, second, cover_count, obstacles


def _pick_pivot(remaining: int, masked: Sequence[MaskedTicket]) -> int:
    """返回可选票据数最少的标签位（minimum remaining values）。"""

    bit = remaining & -remaining
    pivot = bit.bit_length() - 1
    best_count = -1
    r = remaining
    while r:
        b = r & -r
        i = b.bit_length() - 1
        count = sum(
            1 for mt in masked
            if (mt.mask & b) and (mt.mask & ~remaining) == 0
        )
        if best_count == -1 or count < best_count:
            best_count, pivot = count, i
            if count == 0:
                break
        r ^= b
    return pivot


def _total_of(masked: Sequence[MaskedTicket], ticket_ids: Sequence[str]) -> int:
    quantities = {mt.ticket_id: mt.quantity for mt in masked}
    return sum(quantities[t] for t in ticket_ids)


def _diagnose(
    full_mask: int,
    masked: Sequence[MaskedTicket],
    labels: Sequence[str],
    memo: Dict[int, _State],
) -> Tuple[str, ...]:
    """为无覆盖结论生成可复查障碍说明。"""

    obstacles: List[str] = []

    # 障碍一：标签根本没有出现在任何有效票据里。
    uncovered_labels = []
    r = full_mask
    while r:
        b = r & -r
        i = b.bit_length() - 1
        if not any(mt.mask & b for mt in masked):
            uncovered_labels.append(labels[i])
        r ^= b
    if uncovered_labels:
        obstacles.append(
            "标签无任何有效票据覆盖: " + ", ".join(uncovered_labels)
        )

    # 障碍二：沿 MRV 分叉给出“尝试过的票 + 剩余无法闭合的标签”证据链。
    trail: List[str] = []
    remaining = full_mask
    seen: set = set()
    while remaining and remaining not in seen:
        seen.add(remaining)
        pivot = _pick_pivot(remaining, masked)
        candidates = [
            mt for mt in masked
            if (mt.mask & (1 << pivot)) and (mt.mask & ~remaining) == 0
        ]
        if not candidates:
            stuck = [labels[i] for i in range(len(labels)) if remaining & (1 << i)]
            obstacles.append(
                "死路: 已选票据 [" + (", ".join(trail) if trail else "无")
                + "] 后标签 [" + ", ".join(stuck) + "] 找不到可继续且不重叠的票据"
            )
            break
        mt = candidates[0]
        trail.append(mt.ticket_id)
        remaining ^= mt.mask
        if remaining == 0:
            break
        if memo.get(remaining) is not None and memo[remaining].status == Status.NO_COVER:
            continue

    if not obstacles:
        obstacles.append("不存在互不重叠且并集恰好等于全部预期标签的有效票组")
    return tuple(obstacles)
