"""离线盘点入仓器主体。

职责边界
========
- 维护调用方给定的标签目录、原始票据、撤票记录；
- 对输入做严格校验并区分错误类型（未知标签、票内重复标签、内容不一致重送、
  撤票引用不存在等），完全相同的重送幂等处理；
- 按库位 + 轮次取有效票据，用标签重叠关系把库位切成互无共同票据的分量；
- 逐分量调用精确覆盖分析，绝不把各分量的覆盖组合相乘展开；
- 撤票后同一轮次可直接重算，结论与证据随有效票据状态整体切换。
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .cover_analysis import analyze_component
from .errors import (
    DuplicateLabelError,
    InvalidTicketError,
    TicketConflictError,
    UnknownLabelError,
    UnknownLocationError,
    UnknownWithdrawalRefError,
)
from .models import (
    ComponentLimit,
    ComponentResult,
    CoverEvidence,
    LocationResult,
    Status,
    Ticket,
)


class InventoryEngine:
    def __init__(
        self,
        label_catalog: Dict[str, Iterable[str]],
        limit: Optional[ComponentLimit] = None,
    ) -> None:
        """``label_catalog``：``{库位: 该库位的预期标签集合}``。

        入仓器只承认目录内标签，目录之外一律按未知标签拒绝，不会虚构标签。
        """

        self._catalog: Dict[str, FrozenSet[str]] = {}
        for location, labels in label_catalog.items():
            if not isinstance(location, str) or not location:
                raise InvalidTicketError("库位标识必须是非空字符串")
            label_list = list(labels)
            if any(not isinstance(x, str) or not x for x in label_list):
                raise InvalidTicketError(f"库位 {location} 的标签必须是非空字符串")
            if len(set(label_list)) != len(label_list):
                raise InvalidTicketError(f"库位 {location} 的标签目录存在重复标签")
            if not label_list:
                raise InvalidTicketError(f"库位 {location} 的标签目录不能为空")
            self._catalog[location] = frozenset(label_list)

        self._limit = limit or ComponentLimit()
        self._tickets: Dict[str, Ticket] = {}
        self._withdrawn: set = set()

    # ------------------------------------------------------------------
    # 票据与撤票录入
    # ------------------------------------------------------------------
    def submit_ticket(
        self,
        ticket_id: str,
        location: str,
        round_id: str,
        labels: Iterable[str],
        quantity: int,
    ) -> bool:
        """录入一张票。返回 ``True`` 表示新票，``False`` 表示同内容幂等重送。"""

        if not isinstance(ticket_id, str) or not ticket_id:
            raise InvalidTicketError("票 ID 必须是非空字符串")
        if not isinstance(round_id, str) or not round_id:
            raise InvalidTicketError("轮次标记必须是非空字符串")
        if location not in self._catalog:
            raise UnknownLocationError(f"未知库位: {location!r}")
        # bool 是 int 的子类，盘点件数不接受布尔值；浮点件数同样拒绝。
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise InvalidTicketError(
                f"票 {ticket_id} 的件数必须是非负整数，收到: {quantity!r}"
            )

        label_list = list(labels)
        if not label_list:
            raise InvalidTicketError(f"票 {ticket_id} 至少要覆盖一个明确标签")
        if any(not isinstance(x, str) or not x for x in label_list):
            raise InvalidTicketError(f"票 {ticket_id} 的标签必须是非空字符串")
        if len(set(label_list)) != len(label_list):
            raise DuplicateLabelError(
                f"票 {ticket_id} 内存在重复标签: {sorted(label_list)}"
            )
        unknown = sorted(set(label_list) - self._catalog[location])
        if unknown:
            raise UnknownLabelError(
                f"票 {ticket_id} 引用了库位 {location} 标签目录之外的标签: {unknown}"
            )

        ticket = Ticket(
            ticket_id=ticket_id,
            location=location,
            round_id=round_id,
            labels=frozenset(label_list),
            quantity=quantity,
        )

        existing = self._tickets.get(ticket_id)
        if existing is not None:
            if existing != ticket:
                raise TicketConflictError(
                    "同一票 ID 重送但内容不一致: "
                    f"已有={existing.as_dict()}, 新送={ticket.as_dict()}"
                )
            return False  # 完全相同重送：幂等，状态（含是否已撤）保持不变

        self._tickets[ticket_id] = ticket
        return True

    def withdraw(self, ticket_id: str) -> bool:
        """撤销一张票。返回 ``True`` 表示本次生效，``False`` 表示此前已撤（幂等）。"""

        if ticket_id not in self._tickets:
            raise UnknownWithdrawalRefError(f"撤票引用了不存在的票 ID: {ticket_id!r}")
        if ticket_id in self._withdrawn:
            return False
        self._withdrawn.add(ticket_id)
        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def locations(self) -> Tuple[str, ...]:
        return tuple(self._catalog)

    def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        return self._tickets.get(ticket_id)

    def is_withdrawn(self, ticket_id: str) -> bool:
        return ticket_id in self._withdrawn

    def active_tickets(self, location: str, round_id: str) -> List[Ticket]:
        """取某库位某轮次当前仍有效的票据（撤票不计，非撤回事项保留）。"""

        if location not in self._catalog:
            raise UnknownLocationError(f"未知库位: {location!r}")
        return [
            t for t in self._tickets.values()
            if t.location == location
            and t.round_id == round_id
            and t.ticket_id not in self._withdrawn
        ]

    def evaluate(self, location: str, round_id: str) -> LocationResult:
        """判定单个库位在指定轮次下能否入仓。"""

        if location not in self._catalog:
            raise UnknownLocationError(f"未知库位: {location!r}")
        components = self._build_components(location, round_id)
        return self._assemble(location, round_id, components)

    def evaluate_all(self, round_id: str) -> List[LocationResult]:
        """逐库位独立判定；库位之间互不影响，也不要求跨库位历史。"""

        return [self.evaluate(loc, round_id) for loc in self._catalog]

    # ------------------------------------------------------------------
    # 分量切分与汇总
    # ------------------------------------------------------------------
    def _build_components(self, location: str, round_id: str) -> List[ComponentResult]:
        ordered_labels = sorted(self._catalog[location])
        index = {label: i for i, label in enumerate(ordered_labels)}
        parent = list(range(len(ordered_labels)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        active = self.active_tickets(location, round_id)
        for ticket in active:
            ids = [index[label] for label in ticket.labels]
            for other in ids[1:]:
                union(ids[0], other)

        groups: Dict[int, List[int]] = {}
        for i in range(len(ordered_labels)):
            groups.setdefault(find(i), []).append(i)

        results: List[ComponentResult] = []
        for component_index, root in enumerate(sorted(groups)):
            member_ids = groups[root]
            component_labels = tuple(ordered_labels[i] for i in member_ids)
            member_set = frozenset(component_labels)
            component_tickets = [
                t for t in active if t.labels and t.labels <= member_set
            ]

            if (
                len(component_labels) > self._limit.max_labels
                or len(component_tickets) > self._limit.max_tickets
            ):
                results.append(ComponentResult(
                    component_index=component_index,
                    labels=component_labels,
                    status=Status.LIMIT_EXCEEDED,
                    exact_total=None,
                    cover_count=None,
                    evidence=None,
                    second_evidence=None,
                    obstacles=(
                        f"分量规模超限: 标签 {len(component_labels)} "
                        f"(上限 {self._limit.max_labels}) / "
                        f"有效票 {len(component_tickets)} "
                        f"(上限 {self._limit.max_tickets})，拒绝枚举",
                    ),
                    active_ticket_count=len(component_tickets),
                    limit=self._limit,
                ))
                continue

            status, total, evidence, second, cover_count, obstacles = analyze_component(
                component_labels, component_tickets
            )
            results.append(ComponentResult(
                component_index=component_index,
                labels=component_labels,
                status=status,
                exact_total=total,
                cover_count=cover_count,
                evidence=evidence,
                second_evidence=second,
                obstacles=obstacles,
                active_ticket_count=len(component_tickets),
                limit=self._limit,
            ))
        return results

    def _assemble(
        self,
        location: str,
        round_id: str,
        components: Sequence[ComponentResult],
    ) -> LocationResult:
        # 每个分量独立成立；任何一个分量不可入仓，整个库位都不可入仓。
        # 分量细节全部保留，绝不允许用另一个分量的反向差额把歧义“平均掉”。
        precedence = (Status.LIMIT_EXCEEDED, Status.AMBIGUOUS, Status.NO_COVER)
        location_status = Status.ACCEPTED
        for rejected in precedence:
            if any(c.status == rejected for c in components):
                location_status = rejected
                break

        if location_status == Status.ACCEPTED:
            exact_total: Optional[int] = sum(c.exact_total for c in components)
            evidence: Tuple[CoverEvidence, ...] = tuple(
                c.evidence for c in components if c.evidence is not None
            )
        else:
            exact_total = None
            evidence = tuple(
                c.evidence for c in components if c.evidence is not None
            )

        return LocationResult(
            location=location,
            round_id=round_id,
            status=location_status,
            exact_total=exact_total,
            components=tuple(components),
            evidence=evidence,
        )
