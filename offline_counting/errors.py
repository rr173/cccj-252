"""入仓器的显式错误类型。

所有输入问题都用可区分的异常类型表达，调用方可以按类型分别处理，
而不是依赖错误字符串匹配。
"""


class InventoryValidationError(ValueError):
    """所有盘点输入校验错误的基类。"""


class InvalidTicketError(InventoryValidationError):
    """票据结构非法（空覆盖、件数不是非负整数等）。"""


class UnknownLabelError(InventoryValidationError):
    """票据引用了调用方标签目录之外的标签；入仓器绝不虚构标签。"""


class DuplicateLabelError(InventoryValidationError):
    """同一张票内重复声明了同一标签（票内标签必须互不相同）。"""


class UnknownLocationError(InventoryValidationError):
    """引用了标签目录中不存在的库位。"""


class TicketConflictError(InventoryValidationError):
    """同一票 ID 重送但内容（库位/轮次/标签/件数）不一致。"""


class UnknownWithdrawalRefError(InventoryValidationError):
    """撤票记录引用了一张系统中不存在的票。"""
