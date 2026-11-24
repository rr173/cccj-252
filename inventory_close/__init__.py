"""离线库存日结工作区（inventory_close）。

一个工作区核算一个品项，出入库仅来自库位间调拨；盘点锚点来自已核定的
盘点输出（只接受 ``accepted`` 事实），调拨按批次导入、按最高修订号生效，
日结行随批次增量重算，全部状态保存在可关闭重开的 SQLite 文件中。
"""

from .errors import (
    WorkspaceError,
    InvalidConfigError,
    InvalidRecordError,
    VersionConflictError,
    WorkspaceStateError,
)
from .store import (
    init_workspace,
    import_batch,
    read_close,
    list_anchors,
    list_versions,
    list_batches,
    get_batch,
)

__all__ = [
    "init_workspace",
    "import_batch",
    "read_close",
    "list_anchors",
    "list_versions",
    "list_batches",
    "get_batch",
    "WorkspaceError",
    "InvalidConfigError",
    "InvalidRecordError",
    "VersionConflictError",
    "WorkspaceStateError",
]
