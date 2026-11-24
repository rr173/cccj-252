"""只读调拨追溯：把一次盘点开启的存量，沿现行生效调拨追踪到截至日。

本模块不写入工作区，也不改变任何盘点、调拨或日结记录。它只读取同一次
一致读事务中的固定盘点锚点与现行生效调拨，按调用方提供的“作业顺位清单”
模拟库位间的 FIFO 流转，并回答：

- 起点盘点开启了多少件；
- 其中哪些件随哪几笔发货离开、每笔发货各从发货库位的哪次入货取得；
- 截至日还有多少件留在各库位、多少件已发未收、多少件被后续盘点中止追溯。

数量始终以整数批量表示，不按单件或逐条完整路径展开；拆开、汇合只保留
库位内有序的“起点件 / 来源不明件”片段。

作业顺位清单（JSON）示例::

    {"entries": [
      {"doc_no": "T-1", "revision": 2, "side": "ship", "rank": 1},
      {"doc_no": "T-1", "revision": 2, "side": "recv", "rank": 2}
    ]}

也可直接使用数组，或把同一单号修订的发/收顺位写在一行
``{"doc_no": "T-1", "revision": 2, "ship": 1, "recv": 2}``。
清单必须恰好覆盖业务日不晚于截至日的全部现行生效调拨腿；日期一律取
工作区记录，清单只提供顺位。同一业务日内顺位必须唯一，当天发收的单据
发货顺位必须小于收货顺位。
"""

from __future__ import annotations

import datetime as dt
import re
from collections import deque
from typing import Dict, List, Optional, Tuple

from .errors import InvalidRecordError, WorkspaceStateError
from .store import _open_existing

SIDE_SHIP = "ship"
SIDE_RECV = "recv"

KIND_TRANSFER = "transfer"
STATUS_ACCEPTED = "accepted"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1

_SIDE_ALIASES = {
    "ship": SIDE_SHIP,
    "recv": SIDE_RECV,
}

_ENTRY_KEYS = {
    "doc_no", "revision", "side", "rank", "ship", "recv",
}

_MANIFEST_LIST_KEYS = ("entries",)


# ---------------------------------------------------------------- 基础解析


def _parse_date(value, field: str) -> dt.date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise InvalidRecordError(f"{field}: 日期必须为 YYYY-MM-DD，实际为 {value!r}")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise InvalidRecordError(f"{field}: 无效日历日期 {value!r}") from None


def _parse_int(value, field: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRecordError(f"{field}: 必须为整数，实际为 {value!r}")
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise InvalidRecordError(f"{field}: 超出有符号 64 位整数范围：{value!r}")
    if minimum is not None and value < minimum:
        raise InvalidRecordError(f"{field}: 必须 >= {minimum}，实际为 {value!r}")
    return value


def _parse_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRecordError(f"{field}: 必须为非空字符串，实际为 {value!r}")
    return value


def _parse_side(value, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidRecordError(f"{field}: 必须为 'ship' 或 'recv'，实际为 {value!r}")
    side = _SIDE_ALIASES.get(value.strip().lower())
    if side is None:
        raise InvalidRecordError(f"{field}: 必须为 'ship' 或 'recv'，实际为 {value!r}")
    return side


# ---------------------------------------------------------------- 清单读取


def _entry_ranks(entry: dict, index: int) -> Dict[str, int]:
    """把一行清单条目展开为 {side: rank}；同一侧重复给出即报错。

    两种写法任选其一：

    - ``{"side": "ship", "rank": 1}``（或 ``"recv"`` / ``"rank"``）；
    - ``{"ship": 1, "recv": 2}``（一行给同一单号修订的两侧顺位）。
    """
    field = f"manifest[{index}]"
    ranks: Dict[str, int] = {}

    if "side" in entry:
        side = _parse_side(entry["side"], f"{field}.side")
        if "rank" not in entry:
            raise InvalidRecordError(f"{field}: 缺少顺位字段 rank")
        ranks[side] = _parse_int(entry["rank"], f"{field}.rank", minimum=1)

    for key, side in (("ship", SIDE_SHIP), ("recv", SIDE_RECV)):
        if key in entry:
            if side in ranks:
                raise InvalidRecordError(f"{field}: 同一侧的顺位重复给出")
            ranks[side] = _parse_int(entry[key], f"{field}.{key}", minimum=1)

    if not ranks:
        raise InvalidRecordError(
            f"{field}: 必须给出 side + rank，或 ship / recv 顺位"
        )
    return ranks


def parse_manifest(raw) -> Dict[Tuple[str, int], Dict[str, int]]:
    """校验清单结构，返回 {(单号, 修订号): {side: rank}}。

    结构错误（未知字段、同一腿重复、非正整数顺位）在这里一次性报出，
    不交付部分追溯结果。
    """
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        unknown = set(raw) - set(_MANIFEST_LIST_KEYS)
        if unknown:
            raise InvalidRecordError(f"清单含未知字段：{sorted(unknown)}")
        entries = None
        for key in _MANIFEST_LIST_KEYS:
            if key in raw:
                entries = raw[key]
                break
        if entries is None:
            raise InvalidRecordError("清单对象必须含 entries 数组")
    else:
        raise InvalidRecordError("作业顺位清单必须是 JSON 数组或对象")

    if not isinstance(entries, list):
        raise InvalidRecordError("清单条目必须是数组")

    legs: Dict[Tuple[str, int], Dict[str, int]] = {}
    for i, entry in enumerate(entries):
        field = f"manifest[{i}]"
        if not isinstance(entry, dict):
            raise InvalidRecordError(f"{field}: 必须是对象")
        unknown = set(entry) - _ENTRY_KEYS
        if unknown:
            raise InvalidRecordError(f"{field}: 未知字段 {sorted(unknown)}")

        if "doc_no" not in entry:
            raise InvalidRecordError(f"{field}: 缺少单号 doc_no")
        doc_no = _parse_text(entry["doc_no"], f"{field}.doc_no")

        if "revision" not in entry:
            raise InvalidRecordError(f"{field}: 缺少修订号 revision")
        revision = _parse_int(entry["revision"], f"{field}.revision", minimum=1)

        ranks = _entry_ranks(entry, i)
        key = (doc_no, revision)
        bucket = legs.setdefault(key, {})
        for side, rank in ranks.items():
            if side in bucket:
                raise InvalidRecordError(
                    f"{field}: 单号 {doc_no!r} 修订 {revision} 的 {side} 顺位重复"
                )
            bucket[side] = rank
    return legs


# ---------------------------------------------------------------- 快照读取


def _read_snapshot(db_path: str) -> dict:
    """在一个只读事务中读取建账信息、固定盘点与全部调拨版本。"""
    conn = _open_existing(db_path)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            anchors = [
                {
                    "location": loc,
                    "biz_date": day,
                    "round": rnd,
                    "status": status,
                    "exact_total": total,
                }
                for loc, day, rnd, status, total in conn.execute(
                    "SELECT location, biz_date, round, status, exact_total"
                    " FROM anchors ORDER BY location, biz_date"
                )
            ]
            versions: Dict[str, Dict[int, dict]] = {}
            for (doc_no, revision, kind, fl, fd, tl, td, qty) in conn.execute(
                "SELECT doc_no, revision, kind, from_location, from_date,"
                " to_location, to_date, quantity FROM transfer_versions"
            ):
                versions.setdefault(doc_no, {})[revision] = {
                    "kind": kind,
                    "from_location": fl,
                    "from_date": fd,
                    "to_location": tl,
                    "to_date": td,
                    "quantity": qty,
                }
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()

    effective_rev = {doc: max(by_rev) for doc, by_rev in versions.items()}
    effective = {doc: versions[doc][rev] for doc, rev in effective_rev.items()}
    accepted = [a for a in anchors if a["status"] == STATUS_ACCEPTED]
    return {
        "meta": meta,
        "closing_end": dt.date.fromisoformat(meta["closing_end"]),
        "anchors": anchors,
        "accepted": accepted,
        "versions": versions,
        "effective_rev": effective_rev,
        "effective": effective,
    }


# ---------------------------------------------------------------- 范围与清单校验


def _in_scope_legs(snap: dict, cutoff: dt.date) -> Dict[Tuple[str, int, str], dict]:
    required: Dict[Tuple[str, int, str], dict] = {}
    for doc_no, version in snap["effective"].items():
        if version["kind"] != KIND_TRANSFER:
            continue
        revision = snap["effective_rev"][doc_no]
        for side, location, day_key in (
            (SIDE_SHIP, version["from_location"], version["from_date"]),
            (SIDE_RECV, version["to_location"], version["to_date"]),
        ):
            day = dt.date.fromisoformat(day_key)
            if day > cutoff:
                continue
            required[(doc_no, revision, side)] = {
                "doc_no": doc_no,
                "revision": revision,
                "side": side,
                "location": location,
                "biz_date": day,
                "quantity": version["quantity"],
            }
    return required


def _check_manifest(manifest_ranks, required, snap) -> None:
    supplied = set()
    rank_owner: Dict[Tuple[dt.date, int], Tuple[str, int, str]] = {}

    for (doc_no, revision), sides in manifest_ranks.items():
        by_rev = snap["versions"].get(doc_no)
        if by_rev is None:
            raise InvalidRecordError(f"清单引用了工作区不存在的单号 {doc_no!r}")
        if revision not in by_rev:
            raise InvalidRecordError(
                f"清单引用了不存在的修订：单号 {doc_no!r} 修订 {revision}"
            )
        effective_rev = snap["effective_rev"][doc_no]
        if revision != effective_rev:
            raise InvalidRecordError(
                f"清单引用了已失效修订：单号 {doc_no!r} 修订 {revision}"
                f"（现行生效修订 {effective_rev}）"
            )
        version = by_rev[revision]
        if version["kind"] != KIND_TRANSFER:
            raise InvalidRecordError(
                f"清单引用了撤销版本：单号 {doc_no!r} 修订 {revision}（没有调拨腿）"
            )
        for side, rank in sides.items():
            key = (doc_no, revision, side)
            entry = required.get(key)
            if entry is None:
                day = version["from_date"] if side == SIDE_SHIP else version["to_date"]
                raise InvalidRecordError(
                    f"清单包含不在范围内的腿：单号 {doc_no!r} 修订 {revision}"
                    f" {side}（业务日 {day}，超过截至日或该腿不存在）"
                )
            supplied.add(key)
            rank_key = (entry["biz_date"], rank)
            if rank_key in rank_owner:
                other = rank_owner[rank_key]
                raise InvalidRecordError(
                    f"清单顺位重复：{entry['biz_date'].isoformat()} 的顺位 {rank}"
                    f" 同时给了 {other[0]} 修订 {other[1]} {other[2]} 与"
                    f" {doc_no} 修订 {revision} {side}；当日顺位在全仓唯一"
                )
            rank_owner[rank_key] = key

    missing = sorted(set(required) - supplied)
    if missing:
        detail = "；".join(
            f"单号 {doc} 修订 {rev} {side}"
            for doc, rev, side in missing
        )
        raise InvalidRecordError(f"清单缺少范围内生效调拨腿：{detail}")

    # 同日发收的调拨：发货必须先于收货。
    for (doc_no, revision, side), entry in required.items():
        if side != SIDE_SHIP:
            continue
        recv = required.get((doc_no, revision, SIDE_RECV))
        if recv is None or recv["biz_date"] != entry["biz_date"]:
            continue
        ship_rank = manifest_ranks[(doc_no, revision)][SIDE_SHIP]
        recv_rank = manifest_ranks[(doc_no, revision)][SIDE_RECV]
        if ship_rank >= recv_rank:
            raise InvalidRecordError(
                f"同日发收的调拨必须在清单中先发后收：单号 {doc_no!r} 修订"
                f" {revision} 于 {entry['biz_date'].isoformat()} 发收，"
                f"发货顺位 {ship_rank} 必须小于收货顺位 {recv_rank}"
            )


# ---------------------------------------------------------------- 库存片段模型


def _coalesce(runs):
    out: List[list] = []
    for kind, qty in runs:
        if qty <= 0:
            continue
        if out and out[-1][0] == kind:
            out[-1][1] += qty
        else:
            out.append([kind, qty])
    return out


class _Lot:
    """库位内一次入货形成的片段序列。

    片段按实际取用先后排列；``O`` 表示仍可追溯到起点盘点的件，``U`` 表示
    来源不明件。同一次入货的 ``O`` 件数量全部归到该入货记录（起点盘点或
    某笔收货），不再区分更早的路径。
    """

    __slots__ = ("runs", "total", "origin", "label")

    def __init__(self, runs, total: int, origin: int, label):
        self.runs = deque(runs)
        self.total = total
        self.origin = origin
        self.label = label


class _LocationStock:
    __slots__ = ("lots",)

    def __init__(self):
        self.lots = deque()

    def add(self, runs, label) -> None:
        runs = _coalesce(runs)
        if not runs:
            return
        total = sum(q for _, q in runs)
        origin = sum(q for k, q in runs if k == "O")
        self.lots.append(_Lot([list(r) for r in runs], total, origin, label))

    def replace_with(self, kind: str, total: int, label) -> None:
        self.lots.clear()
        if total > 0:
            self.lots.append(_Lot([[kind, total]], total,
                                  total if kind == "O" else 0, label))

    def origin_remaining(self) -> int:
        return sum(lot.origin for lot in self.lots)

    def take(self, amount: int):
        """从最早可用库存取 amount 件。

        返回 ``(取用片段, 各入货起点件数, 实取件数)``。取用片段按原顺序保留，
        跨批取货的顺序即列表顺序；库位不足时，差额由调用方显式补为来源不明，
        并排在实取片段之后。
        """
        taken_runs: List[list] = []
        sources: List[Tuple[tuple, int]] = []
        remaining = amount
        while remaining > 0 and self.lots:
            lot = self.lots[0]
            use = min(lot.total, remaining)
            left = use
            origin_here = 0
            while left > 0:
                run = lot.runs[0]
                qty = min(run[1], left)
                if run[0] == "O":
                    origin_here += qty
                taken_runs.append([run[0], qty])
                run[1] -= qty
                left -= qty
                if run[1] == 0:
                    lot.runs.popleft()
            lot.total -= use
            lot.origin -= origin_here
            if lot.total == 0:
                self.lots.popleft()
            if origin_here > 0:
                sources.append((lot.label, origin_here))
            remaining -= use
        if remaining > 0:
            taken_runs.append(["U", remaining])
        return _coalesce(taken_runs), sources, amount - remaining


# ---------------------------------------------------------------- 事件与模拟


def _anchor_label(anchor) -> tuple:
    return ("anchor", anchor["location"], anchor["biz_date"], anchor["round"])


def _receipt_label(leg) -> tuple:
    return ("receipt", leg["doc_no"], leg["revision"],
            leg["location"], leg["biz_date"].isoformat())


def _source_sort_key(item) -> tuple:
    """来源排序与清单行序无关：起点盘点在前，其余按单号、修订、库位、日期。"""
    label = item[0]
    if label[0] == "anchor":
        return (0, "", 0, label[1], label[2], label[3])
    return (1, label[1], label[2], label[3], label[4], "")


def _source_payload(label, quantity: int) -> dict:
    if label[0] == "anchor":
        _, location, biz_date, round_id = label
        return {
            "source": "anchor",
            "location": location,
            "biz_date": biz_date,
            "round": round_id,
            "quantity": quantity,
        }
    _, doc_no, revision, location, biz_date = label
    return {
        "source": "receipt",
        "doc_no": doc_no,
        "revision": revision,
        "location": location,
        "biz_date": biz_date,
        "quantity": quantity,
    }


def _simulate(snap: dict, start_location: str, start_date: dt.date,
              cutoff: dt.date, legs: List[dict]) -> dict:
    start_key = (start_location, start_date)
    start_anchor = None
    for a in snap["accepted"]:
        if a["location"] == start_location and a["biz_date"] == start_date.isoformat():
            start_anchor = a
            break
    if start_anchor is None:
        same_day = sorted({a["location"] for a in snap["accepted"]
                           if a["biz_date"] == start_date.isoformat()})
        hint = "、".join(same_day) if same_day else "无"
        raise WorkspaceStateError(
            f"起点盘点不存在：库位 {start_location!r} 在 {start_date.isoformat()}"
            f" 没有 accepted 盘点（当日有 accepted 盘点的库位：{hint}）"
        )
    start_total = start_anchor["exact_total"]
    if start_total < 0:
        raise WorkspaceStateError(
            f"起点盘点件数为负（{start_total}）：盘点件数必须是非负整数，"
            "无法作为存量追溯"
        )

    # 事件：同日先按清单顺位处理调拨腿，盘点一律在当天调拨结束后。
    events = []
    for a in snap["accepted"]:
        day = dt.date.fromisoformat(a["biz_date"])
        if day > cutoff:
            continue
        events.append({
            "kind": "anchor", "biz_date": day, "location": a["location"],
            "anchor": a, "is_start": (a["location"], day) == start_key,
        })
    for leg in legs:
        events.append({
            "kind": "leg", "biz_date": leg["biz_date"], "location": leg["location"],
            "leg": leg,
        })

    def event_key(ev):
        if ev["kind"] == "anchor":
            return (ev["biz_date"].toordinal(), 1, 0,
                    0, ev["location"], 0, "")
        leg = ev["leg"]
        return (ev["biz_date"].toordinal(), 0, leg["rank"],
                1 if leg["side"] == SIDE_RECV else 0,
                leg["doc_no"], leg["revision"], leg["side"])

    events.sort(key=event_key)

    stocks: Dict[str, _LocationStock] = {}
    shipments: Dict[Tuple[str, int], dict] = {}
    in_transit: Dict[Tuple[str, int], dict] = {}
    stopped: Dict[Tuple[str, str, str], int] = {}

    def stock_of(location: str) -> _LocationStock:
        stock = stocks.get(location)
        if stock is None:
            stock = _LocationStock()
            stocks[location] = stock
        return stock

    for ev in events:
        day = ev["biz_date"]
        location = ev["location"]

        if ev["kind"] == "anchor":
            anchor = ev["anchor"]
            # accepted 盘点给出的是实物件数；工作区允许存档任意整数，但负数
            # 不能作为实物存量。起点件数必须是非负数量，否则追溯无意义。
            total = anchor["exact_total"]
            stock = stock_of(location)
            on_hand = stock.origin_remaining()
            if ev["is_start"]:
                if stock.origin_remaining():
                    raise AssertionError("起点盘点前不应已有起点货")
                # 起点盘点开启一份新存量：这份存量就是被追溯的起点件数。
                stock.replace_with("O", total, _anchor_label(anchor))
            else:
                # 任何盘点都在当天调拨结束后：盘前未发出的起点货在此中止追溯，
                # 盘后按盘点数开启一份不可追溯的新存量。负盘点数按 0 存量处理。
                if on_hand:
                    key = (location, day.isoformat(), anchor["round"])
                    stopped[key] = stopped.get(key, 0) + on_hand
                stock.replace_with("U", max(total, 0), _anchor_label(anchor))
            continue

        leg = ev["leg"]
        doc_key = (leg["doc_no"], leg["revision"])
        if leg["side"] == SIDE_SHIP:
            stock = stock_of(location)
            runs, sources, _ = stock.take(leg["quantity"])
            origin_taken = sum(q for _, q in sources)
            record = shipments.get(doc_key)
            if record is None:
                version = snap["effective"][leg["doc_no"]]
                record = {
                    "doc_no": leg["doc_no"],
                    "revision": leg["revision"],
                    "from_location": version["from_location"],
                    "from_date": version["from_date"],
                    "to_location": version["to_location"],
                    "to_date": version["to_date"],
                    "quantity": version["quantity"],
                    "origin_quantity": 0,
                    "sources": [],
                }
                shipments[doc_key] = record
            if origin_taken:
                record["origin_quantity"] += origin_taken
                merged = {label: qty for label, qty in record["sources"]}
                for label, qty in sources:
                    merged[label] = merged.get(label, 0) + qty
                record["sources"] = list(merged.items())
            in_transit[doc_key] = {
                "runs": runs,
                "origin": origin_taken,
            }
        else:
            batch = in_transit.pop(doc_key, None)
            if batch is None:
                raise WorkspaceStateError(
                    f"收货腿缺少对应的发货批次：单号 {leg['doc_no']!r}"
                    f" 修订 {leg['revision']}（清单或工作区状态不一致）"
                )
            # 整批以收货时刻进入目的库位；批内保留原发货的取用顺序。
            stock_of(location).add(batch["runs"], _receipt_label(leg))

    return {
        "start_anchor": start_anchor,
        "start_total": start_total,
        "stocks": stocks,
        "shipments": shipments,
        "in_transit": in_transit,
        "stopped": stopped,
    }


# ---------------------------------------------------------------- 报告


def trace_transfers(db_path: str, location: str, biz_date: str, cutoff: str,
                    manifest) -> dict:
    """生成只读调拨追溯报告。

    ``location`` / ``biz_date`` 指定起点盘点，``cutoff`` 为截至日（含）；
    起点必须是截至日已发生的 accepted 盘点，截至日不得晚于工作区末个关账日。
    ``manifest`` 为作业顺位清单（数组或对象），必须恰好覆盖范围内全部生效腿。
    """
    start_date = _parse_date(biz_date, "--date")
    cutoff_date = _parse_date(cutoff, "--cutoff")
    if cutoff_date < start_date:
        raise InvalidRecordError(
            f"截至日不能早于起点盘点日：{cutoff_date.isoformat()}"
            f" < {start_date.isoformat()}"
        )
    manifest_ranks = parse_manifest(manifest)

    snap = _read_snapshot(db_path)
    if cutoff_date > snap["closing_end"]:
        raise InvalidRecordError(
            f"截至日不能超过工作区末个关账日：{cutoff_date.isoformat()}"
            f" > {snap['closing_end'].isoformat()}"
        )
    if location not in snap["effective"] and not any(
        a["location"] == location for a in snap["accepted"]
    ):
        known = sorted({a["location"] for a in snap["accepted"]})
        raise WorkspaceStateError(
            f"库位不是已知库位：{location!r}（已知库位：{'、'.join(known)}）"
        )
    required = _in_scope_legs(snap, cutoff_date)
    _check_manifest(manifest_ranks, required, snap)

    legs = []
    for (doc_no, revision, side), entry in required.items():
        legs.append({
            "doc_no": doc_no,
            "revision": revision,
            "side": side,
            "location": entry["location"],
            "biz_date": entry["biz_date"],
            "quantity": entry["quantity"],
            "rank": manifest_ranks[(doc_no, revision)][side],
        })

    state = _simulate(snap, location, start_date, cutoff_date, legs)

    shipment_rows = []
    for key in sorted(state["shipments"]):
        rec = state["shipments"][key]
        if rec["origin_quantity"] <= 0:
            continue
        shipment_rows.append({
            "doc_no": rec["doc_no"],
            "revision": rec["revision"],
            "from_location": rec["from_location"],
            "from_date": rec["from_date"],
            "to_location": rec["to_location"],
            "to_date": rec["to_date"],
            "quantity": rec["quantity"],
            "origin_quantity": rec["origin_quantity"],
            "sources": [
                _source_payload(label, qty)
                for label, qty in sorted(rec["sources"], key=_source_sort_key)
            ],
        })
    shipment_rows.sort(key=lambda r: (r["from_date"], r["doc_no"], r["revision"]))

    remaining_rows = []
    remaining_total = 0
    for loc in sorted(state["stocks"]):
        qty = state["stocks"][loc].origin_remaining()
        if qty:
            remaining_rows.append({"location": loc, "quantity": qty})
            remaining_total += qty

    in_flight_rows = []
    in_flight_total = 0
    for key in sorted(state["in_transit"]):
        batch = state["in_transit"][key]
        if batch["origin"] <= 0:
            continue
        rec = state["shipments"][key]
        in_flight_rows.append({
            "doc_no": rec["doc_no"],
            "revision": rec["revision"],
            "from_location": rec["from_location"],
            "from_date": rec["from_date"],
            "to_location": rec["to_location"],
            "to_date": rec["to_date"],
            "quantity": batch["origin"],
        })
        in_flight_total += batch["origin"]
    in_flight_rows.sort(key=lambda r: (r["from_date"], r["doc_no"], r["revision"]))

    stopped_rows = []
    stopped_total = 0
    for (loc, day, round_id), qty in state["stopped"].items():
        if not qty:
            continue
        stopped_rows.append({
            "location": loc,
            "biz_date": day,
            "round": round_id,
            "quantity": qty,
        })
        stopped_total += qty
    stopped_rows.sort(key=lambda r: (r["biz_date"], r["location"], r["round"]))

    destination_total = remaining_total + in_flight_total + stopped_total
    anchor = state["start_anchor"]
    return {
        "status": "ok",
        "read_only": True,
        "item": snap["meta"].get("item"),
        "closing_end": snap["closing_end"].isoformat(),
        "start": {
            "location": anchor["location"],
            "biz_date": anchor["biz_date"],
            "round": anchor["round"],
            "quantity": state["start_total"],
        },
        "cutoff": cutoff_date.isoformat(),
        "shipments": shipment_rows,
        "remaining": remaining_rows,
        "in_flight": in_flight_rows,
        "stopped_at_anchor": stopped_rows,
        "totals": {
            "start_quantity": state["start_total"],
            "remaining": remaining_total,
            "in_flight": in_flight_total,
            "stopped_at_anchor": stopped_total,
            "destination_total": destination_total,
            "balanced": destination_total == state["start_total"],
        },
    }
