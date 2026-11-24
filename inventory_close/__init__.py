"""离线库存日结工作区（inventory_close）。

一个工作区核算一个品项，出入库仅来自库位间调拨；盘点锚点来自已核定的
盘点输出（只接受 ``accepted`` 事实），调拨按批次导入、按最高修订号生效，
日结行随批次增量重算，全部状态保存在可关闭重开的 SQLite 文件中。

``trace_transfers`` 另提供只读调拨追溯：沿现行生效调拨追踪一次盘点开启的
存量到截至日，不写入任何盘点、调拨或日结记录。
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
    read_close_at_batch,
    list_anchors,
    list_versions,
    list_batches,
    get_batch,
)
from .trace import parse_manifest, trace_transfers

__all__ = [
    "init_workspace",
    "import_batch",
    "read_close",
    "read_close_at_batch",
    "list_anchors",
    "list_versions",
    "list_batches",
    "get_batch",
    "parse_manifest",
    "trace_transfers",
    "WorkspaceError",
    "InvalidConfigError",
    "InvalidRecordError",
    "VersionConflictError",
    "WorkspaceStateError",
]
