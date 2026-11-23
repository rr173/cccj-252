"""离线盘点入仓器（offline_counting）。

只依据调用方给定的标签目录与真实票据工作：
- 不生成任何虚拟标签；
- 票据只声明整票件数，绝不向单个标签摊分数量；
- 先证明每个预期标签都能被互不重叠的现有票据恰好覆盖，再校验总数一致性；
- 同一库位按标签重叠关系拆成互无共同票据的分量，逐分量判定，不跨分量做组合乘法展开。
"""

from .errors import (
    InventoryValidationError,
    InvalidTicketError,
    UnknownLabelError,
    DuplicateLabelError,
    UnknownLocationError,
    TicketConflictError,
    UnknownWithdrawalRefError,
)
from .models import Ticket, CoverEvidence, ComponentResult, LocationResult, Status
from .engine import InventoryEngine

__all__ = [
    "InventoryEngine",
    "Ticket",
    "CoverEvidence",
    "ComponentResult",
    "LocationResult",
    "Status",
    "InventoryValidationError",
    "InvalidTicketError",
    "UnknownLabelError",
    "DuplicateLabelError",
    "UnknownLocationError",
    "TicketConflictError",
    "UnknownWithdrawalRefError",
]
