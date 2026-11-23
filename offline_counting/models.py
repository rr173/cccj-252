"""盘点领域的数据模型与结果对象。

所有对象均为不可变值对象，方便在撤票重算前后对照证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional, Tuple


class Status(str, Enum):
    """分量/库位的判定结论。"""

    # 所有完整覆盖的总量一致（或不存在覆盖歧义），可入仓
    ACCEPTED = "accepted"
    # 不存在对预期标签的恰好互不重叠覆盖
    NO_COVER = "no_cover"
    # 存在多套完整覆盖，但不同覆盖得到的总件数不一致
    AMBIGUOUS = "ambiguous"
    # 分量规模超过约定上限（标签 > 18 或有效票 > 100），明确拒绝枚举
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(frozen=True)
class Ticket:
    """一张真实盘点票据。

    - ``ticket_id``：不可冒用的票 ID；
    - ``location``：所属库位；
    - ``round_id``：批次内组合资格（盘点轮次）标记，不同轮次不得拼成一次完整盘点；
    - ``labels``：本票明确覆盖的标签集合，票内不允许重复；
    - ``quantity``：覆盖整体的非负整数件数，只对整票有效，禁止向标签摊分。
    """

    ticket_id: str
    location: str
    round_id: str
    labels: FrozenSet[str]
    quantity: int

    def as_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "location": self.location,
            "round_id": self.round_id,
            "labels": sorted(self.labels),
            "quantity": self.quantity,
        }


@dataclass(frozen=True)
class CoverEvidence:
    """一套完整覆盖的可复查证据：票 ID 列表与其总件数。"""

    ticket_ids: Tuple[str, ...]
    total: int

    def as_dict(self) -> dict:
        return {"ticket_ids": list(self.ticket_ids), "total": self.total}


@dataclass(frozen=True)
class ComponentResult:
    """单个分量（互无共同票据的一组标签）的判定结果。

    - 接受时：``exact_total`` 为该分量的精确件数，``evidence`` 至少给出一套有效票组；
    - 无覆盖时：``obstacles`` 给出可复查障碍（哪些标签无票、哪处分叉走入死路）；
    - 歧义时：``evidence`` / ``second_evidence`` 给出两套总量不同的完整覆盖。
    """

    component_index: int
    labels: Tuple[str, ...]
    status: Status
    exact_total: Optional[int]
    cover_count: Optional[int]
    evidence: Optional[CoverEvidence]
    second_evidence: Optional[CoverEvidence]
    obstacles: Tuple[str, ...]
    active_ticket_count: int
    limit: "ComponentLimit"

    def as_dict(self) -> dict:
        return {
            "component_index": self.component_index,
            "labels": list(self.labels),
            "status": self.status.value,
            "exact_total": self.exact_total,
            "cover_count": self.cover_count,
            "evidence": self.evidence.as_dict() if self.evidence else None,
            "second_evidence": (
                self.second_evidence.as_dict() if self.second_evidence else None
            ),
            "obstacles": list(self.obstacles),
            "active_ticket_count": self.active_ticket_count,
            "limit": {"max_labels": self.limit.max_labels,
                      "max_tickets": self.limit.max_tickets},
        }


@dataclass(frozen=True)
class ComponentLimit:
    """单个分量允许的最大规模。"""

    max_labels: int = 18
    max_tickets: int = 100


@dataclass(frozen=True)
class LocationResult:
    """一个库位在指定轮次下的整体判定。

    库位总量（若可入仓）是各分量精确件数之和；这里不会把不同分量的
    覆盖组合做笛卡尔乘积累乘，也不会用一个分量的差额去抵消另一个分量的歧义。
    """

    location: str
    round_id: str
    status: Status
    exact_total: Optional[int]
    components: Tuple[ComponentResult, ...]
    evidence: Tuple[CoverEvidence, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "location": self.location,
            "round_id": self.round_id,
            "status": self.status.value,
            "exact_total": self.exact_total,
            "components": [c.as_dict() for c in self.components],
            "evidence": [e.as_dict() for e in self.evidence],
        }
