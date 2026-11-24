"""离线库存日结工作区的核心存储与增量重算逻辑。

一个工作区只核算一个品项，出入库仅来自库位间调拨。全部状态保存在一个
可关闭重开的 SQLite 文件中：

- ``anchors``：建账时给定的固定盘点锚点（含 ``status``，仅 ``accepted`` 是事实）；
- ``daily_close``：每个已知库位从首个 accepted 锚点到关账末日的逐日关账行，
  含日末件数与最近一次盘点的日期、轮次；
- ``transfer_versions``：收到过的全部调拨版本（含已失效版本，用于冲突判定）；
- ``legs``：当前生效版本的调拨腿流水（发货腿 ``-qty`` / 收货腿 ``+qty``）；
- ``batches`` / ``batch_documents`` / ``batch_changes``：批次审计，仓管可据此
  复查每一批究竟改了哪些日结行。

一致性保证：一次批量导入 = 一个 SQLite 事务。写入途中中断后重开，只能读到
完整的批前或批后状态；调拨版本、调拨腿、日结行、审计记录同批生效，不会留下
半张调拨，也不会让日结和生效版本分属两批。

增量重算：迟到更正只重算“原生效或新生效调拨腿能影响的区间”——从该腿业务日
到下一盘点锚点之前（无下一锚点则到关账末日）；首锚点之前和锚点当天的腿不影响
日结，不扫描对应区间。区间起点前一日的日结值作为基准，逐日累加重放当前生效腿，
净值未变的行不報也不重写。

按批次还原历史日结：以当前日结为基准，把晚于目标批次的 ``batch_changes`` 审计行
按批号倒序逐一撤销（每行回到该批修改前的件数），即还原该批提交完成时工作区实际
展示的日结，并与当前日结对照。批次写入与审计落在同一事务，历史视图永远对应一个
完整提交的批次，不会读到批执行到一半的状态；后续批次只追加新审计行，即使补录
更早业务日的迟到更正，也不改写较早批次对应的历史视图。
"""

from __future__ import annotations

import bisect
import datetime as dt
import os
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

from .errors import (
    InvalidConfigError,
    InvalidRecordError,
    VersionConflictError,
    WorkspaceStateError,
)

SCHEMA_VERSION = "1"
ONE_DAY = dt.timedelta(days=1)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

KIND_TRANSFER = "transfer"
KIND_VOID = "void"
STATUS_ACCEPTED = "accepted"

SIDE_SHIP = "ship"
SIDE_RECV = "recv"

TRANSFER_FIELDS = ("from_location", "from_date", "to_location", "to_date", "quantity")

# 调拨版本内容元组：(kind, from_location, from_date, to_location, to_date, quantity)，
# 与 transfer_versions 的列顺序一致，撤销版本后五位为 None。
Content = Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[int]]

SCHEMA_STATEMENTS = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE anchors (
         location TEXT NOT NULL,
         biz_date TEXT NOT NULL,
         round TEXT NOT NULL,
         status TEXT NOT NULL,
         exact_total INTEGER NOT NULL,
         PRIMARY KEY (location, biz_date)
       )""",
    """CREATE TABLE daily_close (
         location TEXT NOT NULL,
         biz_date TEXT NOT NULL,
         quantity INTEGER NOT NULL,
         anchor_date TEXT NOT NULL,
         anchor_round TEXT NOT NULL,
         PRIMARY KEY (location, biz_date)
       )""",
    """CREATE TABLE transfer_versions (
         doc_no TEXT NOT NULL,
         revision INTEGER NOT NULL,
         kind TEXT NOT NULL,
         from_location TEXT,
         from_date TEXT,
         to_location TEXT,
         to_date TEXT,
         quantity INTEGER,
         batch_id INTEGER NOT NULL,
         PRIMARY KEY (doc_no, revision)
       )""",
    """CREATE TABLE legs (
         doc_no TEXT NOT NULL,
         side TEXT NOT NULL,
         location TEXT NOT NULL,
         biz_date TEXT NOT NULL,
         delta INTEGER NOT NULL,
         PRIMARY KEY (doc_no, side)
       )""",
    "CREATE INDEX idx_legs_location_date ON legs(location, biz_date)",
    """CREATE TABLE batches (
         batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
         imported_at TEXT NOT NULL,
         records_received INTEGER NOT NULL,
         versions_inserted INTEGER NOT NULL
       )""",
    """CREATE TABLE batch_documents (
         batch_id INTEGER NOT NULL,
         doc_no TEXT NOT NULL,
         old_effective_revision INTEGER,
         new_effective_revision INTEGER,
         PRIMARY KEY (batch_id, doc_no)
       )""",
    """CREATE TABLE batch_changes (
         batch_id INTEGER NOT NULL,
         location TEXT NOT NULL,
         biz_date TEXT NOT NULL,
         old_quantity INTEGER NOT NULL,
         new_quantity INTEGER NOT NULL,
         PRIMARY KEY (batch_id, location, biz_date)
       )""",
]


# ---------------------------------------------------------------- 基础校验


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_date(value, field: str, exc) -> dt.date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise exc(f"{field}: 日期必须为 YYYY-MM-DD，实际为 {value!r}")
    try:
        # 格式检查之后再校验真实日历日期（如 2026-02-30 拒绝）
        return dt.date.fromisoformat(value)
    except ValueError:
        raise exc(f"{field}: 无效日历日期 {value!r}") from None


def _parse_int(value, field: str, exc, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise exc(f"{field}: 必须为整数，实际为 {value!r}")
    if not INT64_MIN <= value <= INT64_MAX:
        raise exc(f"{field}: 超出有符号 64 位整数范围：{value!r}")
    if minimum is not None and value < minimum:
        raise exc(f"{field}: 必须 >= {minimum}，实际为 {value!r}")
    return value


def _parse_text(value, field: str, exc) -> str:
    if not isinstance(value, str) or not value.strip():
        raise exc(f"{field}: 必须为非空字符串，实际为 {value!r}")
    return value


def _parse_round(value, field: str, exc) -> str:
    """轮次沿用盘点输出的 round_id：非空字符串或整数，统一按字符串存储。"""
    if isinstance(value, bool):
        raise exc(f"{field}: 轮次必须为非空字符串或整数，实际为 {value!r}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise exc(f"{field}: 轮次必须为非空字符串或整数，实际为 {value!r}")


# ---------------------------------------------------------------- 连接


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None  # 手动控制事务边界
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def _open_existing(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise WorkspaceStateError(f"工作区不存在：{db_path}（请先 init 建账）")
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        conn.close()
        raise WorkspaceStateError(f"不是有效的日结工作区文件：{db_path}") from None
    if row is None:
        conn.close()
        raise WorkspaceStateError(f"不是有效的日结工作区文件：{db_path}")
    return conn


def _read_meta(conn: sqlite3.Connection) -> Dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta"))


# ---------------------------------------------------------------- 建账


def init_workspace(db_path: str, config: dict) -> dict:
    """建账：指定品项、末个关账日与一组固定盘点锚点，并铺出全部日结行。

    锚点取自已核定盘点输出（库位、轮次、status、exact_total）加上业务日；
    只有 ``accepted`` 锚点是事实。有 accepted 锚点的库位即已知库位。
    """
    if not isinstance(config, dict):
        raise InvalidConfigError("建账配置必须是 JSON 对象")
    unknown = set(config) - {"item", "closing_end", "anchors"}
    if unknown:
        raise InvalidConfigError(f"建账配置含未知字段：{sorted(unknown)}")
    item = _parse_text(config.get("item"), "item", InvalidConfigError)
    closing_end = _parse_date(config.get("closing_end"), "closing_end", InvalidConfigError)
    raw_anchors = config.get("anchors")
    if not isinstance(raw_anchors, list) or not raw_anchors:
        raise InvalidConfigError("anchors 必须是非空数组")
    if os.path.exists(db_path):
        raise WorkspaceStateError(f"工作区已存在：{db_path}")

    anchors: List[Tuple[str, str, str, str, int]] = []
    seen = set()
    for i, raw in enumerate(raw_anchors):
        field = f"anchors[{i}]"
        if not isinstance(raw, dict):
            raise InvalidConfigError(f"{field}: 必须是对象")
        required = {"location", "biz_date", "round", "status", "exact_total"}
        missing = required - set(raw)
        extra = set(raw) - required
        if missing:
            raise InvalidConfigError(f"{field}: 缺少字段 {sorted(missing)}")
        if extra:
            raise InvalidConfigError(f"{field}: 未知字段 {sorted(extra)}")
        location = _parse_text(raw["location"], f"{field}.location", InvalidConfigError)
        biz_date = _parse_date(raw["biz_date"], f"{field}.biz_date", InvalidConfigError)
        round_id = _parse_round(raw["round"], f"{field}.round", InvalidConfigError)
        status = _parse_text(raw["status"], f"{field}.status", InvalidConfigError)
        exact_total = _parse_int(raw["exact_total"], f"{field}.exact_total", InvalidConfigError)
        key = (location, biz_date)
        if key in seen:
            raise InvalidConfigError(
                f"{field}: 同一库位同日最多一条锚点（{location} {biz_date.isoformat()}）"
            )
        seen.add(key)
        if status == STATUS_ACCEPTED and biz_date > closing_end:
            raise InvalidConfigError(
                f"{field}: accepted 锚点不晚于关账末日（{biz_date.isoformat()} > {closing_end.isoformat()}）"
            )
        anchors.append((location, biz_date.isoformat(), round_id, status, exact_total))

    accepted_by_loc: Dict[str, List[Tuple[dt.date, str, int]]] = {}
    for location, biz_date, round_id, status, exact_total in anchors:
        if status == STATUS_ACCEPTED:
            accepted_by_loc.setdefault(location, []).append(
                (dt.date.fromisoformat(biz_date), round_id, exact_total)
            )
    if not accepted_by_loc:
        raise InvalidConfigError("没有任何 accepted 锚点：没有已知库位可建账")
    for lst in accepted_by_loc.values():
        lst.sort()

    conn = _connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", SCHEMA_VERSION),
                ("item", item),
                ("closing_end", closing_end.isoformat()),
                ("created_at", _utcnow()),
            ],
        )
        conn.executemany(
            "INSERT INTO anchors(location, biz_date, round, status, exact_total)"
            " VALUES (?,?,?,?,?)",
            anchors,
        )
        # 铺日结行：每库位从首个 accepted 锚点到关账末日逐日一行。
        # 建账时还没有任何调拨，非锚点日的日末件数 = 最近一次锚点的实数。
        rows: List[Tuple[str, str, int, str, str]] = []
        for location, lst in accepted_by_loc.items():
            day = lst[0][0]
            ai = 0
            while day <= closing_end:
                while ai + 1 < len(lst) and lst[ai + 1][0] <= day:
                    ai += 1
                anchor_date, round_id, exact_total = lst[ai]
                rows.append((location, day.isoformat(), exact_total,
                             anchor_date.isoformat(), round_id))
                day += ONE_DAY
        conn.executemany(
            "INSERT INTO daily_close(location, biz_date, quantity, anchor_date, anchor_round)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
        raise
    conn.close()
    return {
        "status": "ok",
        "item": item,
        "closing_end": closing_end.isoformat(),
        "known_locations": sorted(accepted_by_loc),
        "accepted_anchors": sum(len(v) for v in accepted_by_loc.values()),
        "daily_close_rows": len(rows),
    }


# ---------------------------------------------------------------- 调拨记录校验


def _normalize_record(raw, index: int, known_locations, closing_end: dt.date) -> Tuple[str, int, Content]:
    """把一条调拨修订记录校验并规范化为 (单号, 修订号, 内容元组)。"""
    field = f"records[{index}]"
    if not isinstance(raw, dict):
        raise InvalidRecordError(f"{field}: 必须是对象")
    base_keys = {"doc_no", "revision", "type"}
    unknown = set(raw) - base_keys - set(TRANSFER_FIELDS)
    if unknown:
        raise InvalidRecordError(f"{field}: 未知字段 {sorted(unknown)}")
    missing = base_keys - set(raw)
    if missing:
        raise InvalidRecordError(f"{field}: 缺少字段 {sorted(missing)}")

    doc_no = _parse_text(raw["doc_no"], f"{field}.doc_no", InvalidRecordError)
    revision = _parse_int(raw["revision"], f"{field}.revision", InvalidRecordError, minimum=1)
    rtype = raw["type"]

    if rtype == KIND_TRANSFER:
        missing = set(TRANSFER_FIELDS) - set(raw)
        if missing:
            raise InvalidRecordError(f"{field}: transfer 缺少字段 {sorted(missing)}")
        from_loc = _parse_text(raw["from_location"], f"{field}.from_location", InvalidRecordError)
        to_loc = _parse_text(raw["to_location"], f"{field}.to_location", InvalidRecordError)
        if from_loc not in known_locations:
            raise InvalidRecordError(
                f"{field}: 发货库位 {from_loc!r} 不是已知库位（无 accepted 锚点）"
            )
        if to_loc not in known_locations:
            raise InvalidRecordError(
                f"{field}: 收货库位 {to_loc!r} 不是已知库位（无 accepted 锚点）"
            )
        if from_loc == to_loc:
            raise InvalidRecordError(f"{field}: 发货库位与收货库位必须不同")
        from_date = _parse_date(raw["from_date"], f"{field}.from_date", InvalidRecordError)
        to_date = _parse_date(raw["to_date"], f"{field}.to_date", InvalidRecordError)
        if from_date > to_date:
            raise InvalidRecordError(
                f"{field}: 发货日不晚于收货日（{from_date.isoformat()} > {to_date.isoformat()}）"
            )
        if to_date > closing_end:
            raise InvalidRecordError(
                f"{field}: 日期不晚于关账末日（{to_date.isoformat()} > {closing_end.isoformat()}）"
            )
        qty = _parse_int(raw["quantity"], f"{field}.quantity", InvalidRecordError, minimum=1)
        content: Content = (KIND_TRANSFER, from_loc, from_date.isoformat(),
                            to_loc, to_date.isoformat(), qty)
    elif rtype == KIND_VOID:
        extra = set(TRANSFER_FIELDS) & set(raw)
        if extra:
            raise InvalidRecordError(
                f"{field}: 撤销版本不得携带发收货字段 {sorted(extra)}"
            )
        content = (KIND_VOID, None, None, None, None, None)
    else:
        raise InvalidRecordError(
            f"{field}.type: 必须为 'transfer' 或 'void'，实际为 {rtype!r}"
        )
    return doc_no, revision, content


# ---------------------------------------------------------------- 增量重算


def _compute_ranges(dates, anchor_dates, anchor_set, closing_end):
    """由净差异调拨腿的业务日集合，求需要重算的日结区间。

    每条腿的影响区间 = [腿业务日, 下一锚点之前]（无下一锚点则到关账末日）；
    首锚点之前、锚点当天的腿不影响日结，直接跳过。同库位区间合并后返回。
    """
    first = anchor_dates[0]
    spans = []
    for day in dates:
        if day < first or day in anchor_set:
            continue
        i = bisect.bisect_right(anchor_dates, day)
        end = anchor_dates[i] - ONE_DAY if i < len(anchor_dates) else closing_end
        spans.append((day, end))
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + ONE_DAY:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _recompute_range(conn, location, start, end, anchor_date, anchor_round):
    """重算 [start, end] 区间内的日结行，返回实际改变的行列表。

    基准 = 起点前一日的日结值；区间内逐日累加当前生效腿的净额。
    区间内不含锚点日（区间端点由锚点切出），所以纯累加即可。
    净值未变的行不重写、不報。
    """
    base_date = (start - ONE_DAY).isoformat()
    base_qty = conn.execute(
        "SELECT quantity FROM daily_close WHERE location = ? AND biz_date = ?",
        (location, base_date),
    ).fetchone()[0]
    deltas = dict(
        conn.execute(
            "SELECT biz_date, SUM(delta) FROM legs"
            " WHERE location = ? AND biz_date BETWEEN ? AND ? GROUP BY biz_date",
            (location, start.isoformat(), end.isoformat()),
        )
    )
    existing = dict(
        conn.execute(
            "SELECT biz_date, quantity FROM daily_close"
            " WHERE location = ? AND biz_date BETWEEN ? AND ?",
            (location, start.isoformat(), end.isoformat()),
        )
    )
    changes = []
    updates = []
    qty = base_qty
    day = start
    while day <= end:
        ds = day.isoformat()
        qty += deltas.get(ds, 0)
        old_qty = existing[ds]
        if old_qty != qty:
            updates.append((qty, location, ds))
            changes.append({
                "location": location,
                "biz_date": ds,
                "old_quantity": old_qty,
                "new_quantity": qty,
                "anchor_date": anchor_date.isoformat(),
                "anchor_round": anchor_round,
            })
        day += ONE_DAY
    conn.executemany(
        "UPDATE daily_close SET quantity = ? WHERE location = ? AND biz_date = ?",
        updates,
    )
    return base_date, base_qty, changes


# ---------------------------------------------------------------- 批量导入


def import_batch(db_path: str, payload: dict) -> dict:
    """批量导入调拨修订；任一记录无效或冲突时整批不留痕。

    返回本批真正改变的日结行（含修改前后件数，按库位再按日期排序），
    以及本批涉及的单号、锚点、扫描区间与基准值。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise InvalidRecordError("批次必须是含 records 数组的 JSON 对象")
    raw_records = payload["records"]

    conn = _open_existing(db_path)
    try:
        meta = _read_meta(conn)
        closing_end = dt.date.fromisoformat(meta["closing_end"])
        known_locations = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT location FROM anchors WHERE status = ?",
                (STATUS_ACCEPTED,),
            )
        }
        # 先纯校验全部记录（不碰库），再开事务
        normalized = [
            _normalize_record(raw, i, known_locations, closing_end)
            for i, raw in enumerate(raw_records)
        ]

        conn.execute("BEGIN IMMEDIATE")
        try:
            # 批内去重 / 冲突：同单号同修订号同内容只算一次，异内容即冲突
            batch_versions: Dict[Tuple[str, int], Content] = {}
            for doc_no, revision, content in normalized:
                key = (doc_no, revision)
                if key in batch_versions:
                    if batch_versions[key] != content:
                        raise VersionConflictError(
                            f"批内冲突：单号 {doc_no!r} 修订号 {revision} 内容不一致"
                        )
                else:
                    batch_versions[key] = content

            # 与库内已收版本比对：同内容可重送（幂等），异内容是冲突——
            # 哪怕该版本已不生效
            to_insert: List[Tuple[str, int, Content]] = []
            for (doc_no, revision), content in sorted(batch_versions.items()):
                row = conn.execute(
                    "SELECT kind, from_location, from_date, to_location, to_date, quantity"
                    " FROM transfer_versions WHERE doc_no = ? AND revision = ?",
                    (doc_no, revision),
                ).fetchone()
                if row is not None:
                    if tuple(row) != content:
                        raise VersionConflictError(
                            f"版本冲突：单号 {doc_no!r} 修订号 {revision} 与已收内容不一致"
                            "（即使该版本已不生效也不允许）"
                        )
                else:
                    to_insert.append((doc_no, revision, content))

            # 本批涉及单号在批前的生效修订号（收到过的最高修订号）
            docs = sorted({doc_no for doc_no, _ in batch_versions})
            old_effective: Dict[str, Optional[int]] = {}
            for doc_no in docs:
                old_effective[doc_no] = conn.execute(
                    "SELECT MAX(revision) FROM transfer_versions WHERE doc_no = ?",
                    (doc_no,),
                ).fetchone()[0]

            cur = conn.execute(
                "INSERT INTO batches(imported_at, records_received, versions_inserted)"
                " VALUES (?,?,?)",
                (_utcnow(), len(raw_records), len(to_insert)),
            )
            batch_id = cur.lastrowid

            conn.executemany(
                "INSERT INTO transfer_versions(doc_no, revision, kind, from_location,"
                " from_date, to_location, to_date, quantity, batch_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (doc_no, revision, *content, batch_id)
                    for doc_no, revision, content in to_insert
                ],
            )

            # 生效版本变化 → 调拨腿净差异。版本是整单替换：旧生效腿整体撤下，
            # 新生效腿整体挂上；合法旧版本补录不影响生效版本。
            diff: Dict[Tuple[str, str], int] = {}
            doc_reports = []
            for doc_no in docs:
                new_effective = conn.execute(
                    "SELECT MAX(revision) FROM transfer_versions WHERE doc_no = ?",
                    (doc_no,),
                ).fetchone()[0]
                changed = new_effective != old_effective[doc_no]
                if changed:
                    old_legs = conn.execute(
                        "SELECT side, location, biz_date, delta FROM legs WHERE doc_no = ?",
                        (doc_no,),
                    ).fetchall()
                    conn.execute("DELETE FROM legs WHERE doc_no = ?", (doc_no,))
                    version = conn.execute(
                        "SELECT kind, from_location, from_date, to_location, to_date, quantity"
                        " FROM transfer_versions WHERE doc_no = ? AND revision = ?",
                        (doc_no, new_effective),
                    ).fetchone()
                    new_legs: List[Tuple[str, str, str, int]] = []
                    if version[0] == KIND_TRANSFER:
                        _, from_loc, from_date, to_loc, to_date, qty = version
                        new_legs = [
                            (SIDE_SHIP, from_loc, from_date, -qty),
                            (SIDE_RECV, to_loc, to_date, qty),
                        ]
                        conn.executemany(
                            "INSERT INTO legs(doc_no, side, location, biz_date, delta)"
                            " VALUES (?,?,?,?,?)",
                            [
                                (doc_no, side, loc, day, delta)
                                for side, loc, day, delta in new_legs
                            ],
                        )
                    for _, loc, day, delta in old_legs:
                        key = (loc, day)
                        diff[key] = diff.get(key, 0) - delta
                    for _, loc, day, delta in new_legs:
                        key = (loc, day)
                        diff[key] = diff.get(key, 0) + delta
                doc_reports.append({
                    "doc_no": doc_no,
                    "old_effective_revision": old_effective[doc_no],
                    "new_effective_revision": new_effective,
                    "effective_changed": changed,
                })

            # 净额为零的腿丢弃（净值未变不扫描）；按库位归集差异业务日
            loc_dates: Dict[str, set] = {}
            for (location, day), delta in diff.items():
                if delta == 0:
                    continue
                loc_dates.setdefault(location, set()).add(dt.date.fromisoformat(day))

            changes: List[dict] = []
            scanned_ranges: List[dict] = []
            used_anchors: Dict[Tuple[str, dt.date], str] = {}
            baselines: List[dict] = []
            for location in sorted(loc_dates):
                anchor_rows = conn.execute(
                    "SELECT biz_date, round FROM anchors"
                    " WHERE location = ? AND status = ? ORDER BY biz_date",
                    (location, STATUS_ACCEPTED),
                ).fetchall()
                anchor_dates = [dt.date.fromisoformat(r[0]) for r in anchor_rows]
                anchor_set = set(anchor_dates)
                round_of = {dt.date.fromisoformat(r[0]): r[1] for r in anchor_rows}
                for start, end in _compute_ranges(
                    loc_dates[location], anchor_dates, anchor_set, closing_end
                ):
                    scanned_ranges.append({
                        "location": location,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                    })
                    ai = bisect.bisect_right(anchor_dates, start) - 1
                    anchor_date = anchor_dates[ai]
                    anchor_round = round_of[anchor_date]
                    used_anchors[(location, anchor_date)] = anchor_round
                    base_date, base_qty, range_changes = _recompute_range(
                        conn, location, start, end, anchor_date, anchor_round
                    )
                    baselines.append({
                        "location": location,
                        "biz_date": base_date,
                        "quantity": base_qty,
                    })
                    changes.extend(range_changes)

            changes.sort(key=lambda c: (c["location"], c["biz_date"]))

            conn.executemany(
                "INSERT INTO batch_documents(batch_id, doc_no, old_effective_revision,"
                " new_effective_revision) VALUES (?,?,?,?)",
                [
                    (batch_id, r["doc_no"], r["old_effective_revision"],
                     r["new_effective_revision"])
                    for r in doc_reports
                ],
            )
            conn.executemany(
                "INSERT INTO batch_changes(batch_id, location, biz_date, old_quantity,"
                " new_quantity) VALUES (?,?,?,?,?)",
                [
                    (batch_id, c["location"], c["biz_date"],
                     c["old_quantity"], c["new_quantity"])
                    for c in changes
                ],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "status": "ok",
        "batch_id": batch_id,
        "records_received": len(raw_records),
        "versions_inserted": len(to_insert),
        "documents": doc_reports,
        "scanned_ranges": scanned_ranges,
        "anchors": [
            {"location": loc, "biz_date": day.isoformat(), "round": round_id}
            for (loc, day), round_id in sorted(used_anchors.items())
        ],
        "baselines": sorted(baselines, key=lambda b: (b["location"], b["biz_date"])),
        "changed_rows": changes,
    }


# ---------------------------------------------------------------- 读取


def read_close(db_path: str, location: Optional[str] = None,
               date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    """读取当前日结：库位、业务日、日末件数、最近一次盘点的日期及轮次。"""
    conn = _open_existing(db_path)
    try:
        meta = _read_meta(conn)
        sql = ("SELECT location, biz_date, quantity, anchor_date, anchor_round"
               " FROM daily_close")
        cond, args = [], []
        if location is not None:
            cond.append("location = ?")
            args.append(location)
        if date_from is not None:
            cond.append("biz_date >= ?")
            args.append(_parse_date(date_from, "--from", InvalidRecordError).isoformat())
        if date_to is not None:
            cond.append("biz_date <= ?")
            args.append(_parse_date(date_to, "--to", InvalidRecordError).isoformat())
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY location, biz_date"
        rows = [
            {
                "location": loc,
                "biz_date": day,
                "quantity": qty,
                "anchor_date": a_date,
                "anchor_round": a_round,
            }
            for loc, day, qty, a_date, a_round in conn.execute(sql, args)
        ]
        return {
            "item": meta["item"],
            "closing_end": meta["closing_end"],
            "rows": rows,
        }
    finally:
        conn.close()


def read_close_at_batch(db_path: str, batch_id: int, location: Optional[str] = None,
                        date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    """按批次还原历史日结：该批提交完成时工作区展示的日结，与当前日结对照。

    历史状态由审计表推出：以当前日结为基准，把晚于该批的 ``batch_changes``
    按批号倒序撤销（每行回到该批修改前的件数）。还原结果永远对应一个完整
    提交的批次；后续批次（哪怕补录更早业务日的迟到更正）只追加新审计行，
    不改写较早批次的历史视图。

    每行含当时件数 ``quantity_then``、当前件数 ``quantity_now`` 与差值
    ``delta``（= 当前 − 当时），按库位、业务日排序；可按库位与业务日期
    范围过滤。只接受已成功提交的批次；未知批次、无效日期范围或不存在的
    库位明确报错，不返回看似成功的空结果。
    """
    conn = _open_existing(db_path)
    try:
        meta = _read_meta(conn)
        if isinstance(batch_id, bool) or not isinstance(batch_id, int) or batch_id < 1:
            raise InvalidRecordError(f"批次号必须为正整数，实际为 {batch_id!r}")
        batch = conn.execute(
            "SELECT batch_id, imported_at FROM batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise WorkspaceStateError(
                f"批次不存在：{batch_id}（只接受已成功提交的批次）"
            )
        date_from_v = (_parse_date(date_from, "--from", InvalidRecordError)
                       if date_from is not None else None)
        date_to_v = (_parse_date(date_to, "--to", InvalidRecordError)
                     if date_to is not None else None)
        if date_from_v is not None and date_to_v is not None and date_from_v > date_to_v:
            raise InvalidRecordError(
                f"无效日期范围：--from {date_from_v.isoformat()} 晚于"
                f" --to {date_to_v.isoformat()}"
            )
        known_locations = {
            r[0] for r in conn.execute("SELECT DISTINCT location FROM daily_close")
        }
        if location is not None and location not in known_locations:
            known_txt = "、".join(sorted(known_locations))
            raise WorkspaceStateError(
                f"库位不存在：{location!r}（已知库位：{known_txt}）"
            )

        cond, args = [], []
        if location is not None:
            cond.append("location = ?")
            args.append(location)
        if date_from_v is not None:
            cond.append("biz_date >= ?")
            args.append(date_from_v.isoformat())
        if date_to_v is not None:
            cond.append("biz_date <= ?")
            args.append(date_to_v.isoformat())
        where = " WHERE " + " AND ".join(cond) if cond else ""
        and_cond = " AND " + " AND ".join(cond) if cond else ""

        current = conn.execute(
            "SELECT location, biz_date, quantity, anchor_date, anchor_round"
            " FROM daily_close" + where + " ORDER BY location, biz_date",
            args,
        ).fetchall()
        # 晚于该批的审计改动按批号倒序撤销：同一行被多批改过时，最后写入的
        # 是批号最小（最接近该批）那批的修改前件数，即该批提交完成时的值。
        later = conn.execute(
            "SELECT location, biz_date, old_quantity FROM batch_changes"
            " WHERE batch_id > ?" + and_cond + " ORDER BY batch_id DESC",
            [batch_id] + args,
        ).fetchall()
        then_qty = {(loc, day): qty for loc, day, qty, _, _ in current}
        for loc, day, old in later:
            key = (loc, day)
            if key in then_qty:
                then_qty[key] = old
        rows = [
            {
                "location": loc,
                "biz_date": day,
                "quantity_then": then_qty[(loc, day)],
                "quantity_now": qty,
                "delta": qty - then_qty[(loc, day)],
                "anchor_date": a_date,
                "anchor_round": a_round,
            }
            for loc, day, qty, a_date, a_round in current
        ]
        return {
            "item": meta["item"],
            "closing_end": meta["closing_end"],
            "batch_id": batch[0],
            "imported_at": batch[1],
            "rows": rows,
        }
    finally:
        conn.close()


def list_anchors(db_path: str) -> dict:
    """列出建账时的全部盘点锚点（含非 accepted，仅 accepted 是事实）。"""
    conn = _open_existing(db_path)
    try:
        rows = [
            {
                "location": loc,
                "biz_date": day,
                "round": round_id,
                "status": status,
                "exact_total": total,
            }
            for loc, day, round_id, status, total in conn.execute(
                "SELECT location, biz_date, round, status, exact_total"
                " FROM anchors ORDER BY location, biz_date"
            )
        ]
        return {"anchors": rows}
    finally:
        conn.close()


def list_versions(db_path: str, doc_no: Optional[str] = None) -> dict:
    """列出收到过的调拨版本；effective 标记当前生效（最高修订号）版本。"""
    conn = _open_existing(db_path)
    try:
        sql = (
            "SELECT v.doc_no, v.revision, v.kind, v.from_location, v.from_date,"
            " v.to_location, v.to_date, v.quantity, v.batch_id,"
            " (v.revision = (SELECT MAX(revision) FROM transfer_versions"
            "               WHERE doc_no = v.doc_no)) AS effective"
            " FROM transfer_versions v"
        )
        args: list = []
        if doc_no is not None:
            sql += " WHERE v.doc_no = ?"
            args.append(doc_no)
        sql += " ORDER BY v.doc_no, v.revision"
        rows = [
            {
                "doc_no": d,
                "revision": rev,
                "type": kind,
                "from_location": fl,
                "from_date": fd,
                "to_location": tl,
                "to_date": td,
                "quantity": qty,
                "batch_id": bid,
                "effective": bool(eff),
            }
            for d, rev, kind, fl, fd, tl, td, qty, bid, eff in conn.execute(sql, args)
        ]
        return {"versions": rows}
    finally:
        conn.close()


def list_batches(db_path: str) -> dict:
    """批次列表：每批的记录数、新入库版本数与实际改变的日结行数。"""
    conn = _open_existing(db_path)
    try:
        rows = [
            {
                "batch_id": bid,
                "imported_at": ts,
                "records_received": rec,
                "versions_inserted": ins,
                "changed_rows": changed,
            }
            for bid, ts, rec, ins, changed in conn.execute(
                "SELECT b.batch_id, b.imported_at, b.records_received, b.versions_inserted,"
                " (SELECT COUNT(*) FROM batch_changes c WHERE c.batch_id = b.batch_id)"
                " FROM batches b ORDER BY b.batch_id"
            )
        ]
        return {"batches": rows}
    finally:
        conn.close()


def get_batch(db_path: str, batch_id: int) -> dict:
    """批次明细：涉及单号的生效修订号变化，以及本批改过的日结行。"""
    conn = _open_existing(db_path)
    try:
        batch = conn.execute(
            "SELECT batch_id, imported_at, records_received, versions_inserted"
            " FROM batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise WorkspaceStateError(f"批次不存在：{batch_id}")
        documents = [
            {
                "doc_no": d,
                "old_effective_revision": old,
                "new_effective_revision": new,
            }
            for d, old, new in conn.execute(
                "SELECT doc_no, old_effective_revision, new_effective_revision"
                " FROM batch_documents WHERE batch_id = ? ORDER BY doc_no",
                (batch_id,),
            )
        ]
        changes = [
            {
                "location": loc,
                "biz_date": day,
                "old_quantity": old,
                "new_quantity": new,
            }
            for loc, day, old, new in conn.execute(
                "SELECT location, biz_date, old_quantity, new_quantity"
                " FROM batch_changes WHERE batch_id = ? ORDER BY location, biz_date",
                (batch_id,),
            )
        ]
        return {
            "batch": {
                "batch_id": batch[0],
                "imported_at": batch[1],
                "records_received": batch[2],
                "versions_inserted": batch[3],
            },
            "documents": documents,
            "changed_rows": changes,
        }
    finally:
        conn.close()
