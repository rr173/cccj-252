"""日结工作区的显式错误类型。

所有输入 / 状态问题都用可区分的异常类型表达；任何一类错误在导入途中
抛出都会让整批回滚、不留痕。
"""


class WorkspaceError(ValueError):
    """日结工作区所有业务错误的基类。"""


class InvalidConfigError(WorkspaceError):
    """建账配置非法（品项、关账末日、锚点结构或锚点约束）。"""


class InvalidRecordError(WorkspaceError):
    """调拨修订记录非法（单号、修订号、库位、日期、件数、类型等）。"""


class VersionConflictError(WorkspaceError):
    """相同单号 + 修订号重送内容不一致（哪怕该版本已不生效）。"""


class WorkspaceStateError(WorkspaceError):
    """工作区状态错误（重复建账、文件不存在、非工作区文件等）。"""
