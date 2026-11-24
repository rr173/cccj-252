"""离线库存日结工作区的测试。

运行方式（隔离模式，避免从工作目录加载到同名模块）::

    python3 -I tests/test_inventory_close.py
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inventory_close import (  # noqa: E402
    InvalidConfigError,
    InvalidRecordError,
    VersionConflictError,
    WorkspaceStateError,
    get_batch,
    import_batch,
    init_workspace,
    list_batches,
    list_versions,
    read_close,
    read_close_at_batch,
)
from inventory_close.cli import main as cli_main  # noqa: E402


def base_config():
    """两个已知库位：A 一个锚点，B 两个锚点（跨盘点日场景）。"""
    return {
        "item": "SKU-0001",
        "closing_end": "2026-01-10",
        "anchors": [
            {"location": "A", "biz_date": "2026-01-01", "round": "R1",
             "status": "accepted", "exact_total": 100},
            {"location": "B", "biz_date": "2026-01-01", "round": "R1",
             "status": "accepted", "exact_total": 0},
            {"location": "B", "biz_date": "2026-01-06", "round": "R2",
             "status": "accepted", "exact_total": 7},
        ],
    }


def transfer(doc_no, revision, from_loc, from_date, to_loc, to_date, quantity):
    return {
        "doc_no": doc_no, "revision": revision, "type": "transfer",
        "from_location": from_loc, "from_date": from_date,
        "to_location": to_loc, "to_date": to_date, "quantity": quantity,
    }


def void(doc_no, revision):
    return {"doc_no": doc_no, "revision": revision, "type": "void"}


def batch(*records):
    return {"records": list(records)}


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "ws.db")

    def init_base(self):
        return init_workspace(self.db, base_config())

    def close_map(self):
        rows = read_close(self.db)["rows"]
        return {(r["location"], r["biz_date"]): (r["quantity"], r["anchor_date"], r["anchor_round"])
                for r in rows}

    def qty(self, location, day):
        return self.close_map()[(location, day)][0]


class InitTests(WorkspaceTestCase):
    def test_materializes_daily_close_from_first_anchor_to_closing_end(self):
        result = self.init_base()
        self.assertEqual(result["known_locations"], ["A", "B"])
        self.assertEqual(result["daily_close_rows"], 20)
        rows = self.close_map()
        self.assertEqual(len(rows), 20)
        # 没有任何调拨时：锚点日取盘点数，其余日 = 最近一次锚点实数
        for day in range(1, 11):
            self.assertEqual(rows[("A", f"2026-01-{day:02d}")], (100, "2026-01-01", "R1"))
        for day in range(1, 6):
            self.assertEqual(rows[("B", f"2026-01-{day:02d}")], (0, "2026-01-01", "R1"))
        for day in range(6, 11):
            self.assertEqual(rows[("B", f"2026-01-{day:02d}")], (7, "2026-01-06", "R2"))

    def test_rejects_duplicate_anchor_same_location_same_day(self):
        config = base_config()
        config["anchors"].append(
            {"location": "A", "biz_date": "2026-01-01", "round": "R9",
             "status": "pending", "exact_total": 1})
        with self.assertRaises(InvalidConfigError):
            init_workspace(self.db, config)
        self.assertFalse(os.path.exists(self.db))

    def test_rejects_accepted_anchor_after_closing_end(self):
        config = base_config()
        config["anchors"].append(
            {"location": "A", "biz_date": "2026-01-11", "round": "R3",
             "status": "accepted", "exact_total": 1})
        with self.assertRaises(InvalidConfigError):
            init_workspace(self.db, config)

    def test_non_accepted_anchor_is_not_fact_and_not_known_location(self):
        config = base_config()
        config["anchors"].append(
            {"location": "C", "biz_date": "2026-01-02", "round": "R1",
             "status": "ambiguous", "exact_total": 5})
        result = init_workspace(self.db, config)
        self.assertEqual(result["known_locations"], ["A", "B"])
        # C 不是已知库位：调拨引用即整批拒绝
        with self.assertRaises(InvalidRecordError):
            import_batch(self.db, batch(
                transfer("T-1", 1, "A", "2026-01-03", "C", "2026-01-03", 1)))

    def test_rejects_bad_config(self):
        bad_configs = [
            {"closing_end": "2026-01-10", "anchors": [{"location": "A"}]},  # 缺 item
            {"item": "X", "closing_end": "2026-1-10", "anchors": []},       # 日期格式
            {"item": "X", "closing_end": "2026-02-30", "anchors": []},      # 无效日历日
            {"item": "X", "closing_end": "2026-01-10", "anchors": []},      # 空锚点
            {"item": "X", "closing_end": "2026-01-10", "anchors": [
                {"location": "A", "biz_date": "2026-01-01", "round": "R1",
                 "status": "pending", "exact_total": 1}]},                  # 无 accepted
            {"item": "X", "closing_end": "2026-01-10", "anchors": [
                {"location": "A", "biz_date": "2026-01-01", "round": "R1",
                 "status": "accepted", "exact_total": True}]},              # bool 件数
            {"item": "X", "closing_end": "2026-01-10", "anchors": [
                {"location": "A", "biz_date": "2026-01-01", "round": "R1",
                 "status": "accepted", "exact_total": 1, "extra": 1}]},     # 未知字段
        ]
        for i, config in enumerate(bad_configs):
            db = os.path.join(self._tmp.name, f"bad{i}.db")
            with self.assertRaises(InvalidConfigError, msg=f"config[{i}]"):
                init_workspace(db, config)
            self.assertFalse(os.path.exists(db))

    def test_rejects_existing_workspace(self):
        self.init_base()
        with self.assertRaises(WorkspaceStateError):
            init_workspace(self.db, base_config())

    def test_open_missing_workspace(self):
        with self.assertRaises(WorkspaceStateError):
            read_close(self.db)
        with self.assertRaises(WorkspaceStateError):
            import_batch(self.db, batch())


class ImportTests(WorkspaceTestCase):
    def test_basic_transfer_books_both_ends_on_own_dates(self):
        self.init_base()
        result = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        changed = {(c["location"], c["biz_date"]): (c["old_quantity"], c["new_quantity"])
                   for c in result["changed_rows"]}
        expected = {("A", f"2026-01-{d:02d}"): (100, 90) for d in range(3, 11)}
        expected.update({("B", "2026-01-04"): (0, 10), ("B", "2026-01-05"): (0, 10)})
        self.assertEqual(changed, expected)
        # B 的 01-06 是锚点日：盘点数 7 直接覆盖，不叠加当日之前的在途
        self.assertEqual(self.qty("B", "2026-01-06"), 7)
        self.assertEqual(self.qty("A", "2026-01-03"), 90)
        # 审计落库
        detail = get_batch(self.db, result["batch_id"])
        self.assertEqual(len(detail["changed_rows"]), 10)
        self.assertEqual(detail["documents"][0]["doc_no"], "T-1")

    def test_receipt_date_correction_across_anchor_day(self):
        """跨盘点日的收货日更正：旧腿影响区间到新锚点前，新腿从新区间起算。"""
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        result = import_batch(self.db, batch(
            transfer("T-1", 2, "A", "2026-01-03", "B", "2026-01-07", 10)))
        # 发货腿未变（净额为零）→ A 不扫描；B 只扫两个受影响区间
        self.assertEqual(result["scanned_ranges"], [
            {"location": "B", "from": "2026-01-04", "to": "2026-01-05"},
            {"location": "B", "from": "2026-01-07", "to": "2026-01-10"},
        ])
        self.assertEqual(
            {(a["location"], a["biz_date"]) for a in result["anchors"]},
            {("B", "2026-01-01"), ("B", "2026-01-06")})
        self.assertEqual(
            {(b["location"], b["biz_date"], b["quantity"]) for b in result["baselines"]},
            {("B", "2026-01-03", 0), ("B", "2026-01-06", 7)})
        changed = [(c["location"], c["biz_date"], c["old_quantity"], c["new_quantity"])
                   for c in result["changed_rows"]]
        self.assertEqual(changed, [
            ("B", "2026-01-04", 10, 0),
            ("B", "2026-01-05", 10, 0),
            ("B", "2026-01-07", 7, 17),
            ("B", "2026-01-08", 7, 17),
            ("B", "2026-01-09", 7, 17),
            ("B", "2026-01-10", 7, 17),
        ])
        # 锚点日本身不动
        self.assertEqual(self.qty("B", "2026-01-06"), 7)

    def test_idempotent_resend_changes_nothing(self):
        self.init_base()
        payload = batch(transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10))
        import_batch(self.db, payload)
        before = self.close_map()
        result = import_batch(self.db, payload)  # 完全相同的重送
        self.assertEqual(result["versions_inserted"], 0)
        self.assertEqual(result["changed_rows"], [])
        self.assertEqual(self.close_map(), before)

    def test_conflict_on_same_revision_different_content(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        before = self.close_map()
        with self.assertRaises(VersionConflictError):
            import_batch(self.db, batch(
                transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 12)))
        self.assertEqual(self.close_map(), before)

    def test_conflict_even_when_version_no_longer_effective(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        import_batch(self.db, batch(
            transfer("T-1", 2, "A", "2026-01-03", "B", "2026-01-04", 20)))
        # rev1 已不生效，但异内容重送仍是冲突
        with self.assertRaises(VersionConflictError):
            import_batch(self.db, batch(
                transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-05", 10)))

    def test_failed_batch_leaves_no_trace(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        before_close = self.close_map()
        before_versions = list_versions(self.db)["versions"]
        before_batches = list_batches(self.db)["batches"]
        # 批内一条合法新记录 + 一条冲突记录 → 整批不留痕
        with self.assertRaises(VersionConflictError):
            import_batch(self.db, batch(
                transfer("T-2", 1, "A", "2026-01-05", "B", "2026-01-05", 3),
                transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 99),
            ))
        self.assertEqual(self.close_map(), before_close)
        self.assertEqual(list_versions(self.db)["versions"], before_versions)
        self.assertEqual(list_batches(self.db)["batches"], before_batches)
        # 批内冲突（同单号同修订号异内容）同样整批拒绝
        with self.assertRaises(VersionConflictError):
            import_batch(self.db, batch(
                transfer("T-3", 1, "A", "2026-01-05", "B", "2026-01-05", 3),
                transfer("T-3", 1, "A", "2026-01-05", "B", "2026-01-05", 4),
            ))
        self.assertEqual(list_versions(self.db)["versions"], before_versions)

    def test_invalid_record_leaves_no_trace(self):
        self.init_base()
        before_batches = list_batches(self.db)["batches"]
        with self.assertRaises(InvalidRecordError):
            import_batch(self.db, batch(
                transfer("T-9", 1, "A", "2026-01-05", "B", "2026-01-05", 3),
                transfer("T-8", 1, "A", "2026-01-05", "UNKNOWN", "2026-01-05", 3),
            ))
        self.assertEqual(list_batches(self.db)["batches"], before_batches)
        self.assertEqual(list_versions(self.db)["versions"], [])

    def test_backfill_older_revision_does_not_override(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 3, "A", "2026-01-03", "B", "2026-01-04", 10)))
        before = self.close_map()
        result = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 55)))
        self.assertEqual(result["versions_inserted"], 1)  # 旧版本补录入库
        self.assertEqual(result["changed_rows"], [])      # 但不覆盖新版本
        self.assertEqual(self.close_map(), before)
        versions = list_versions(self.db, doc_no="T-1")["versions"]
        effective = {v["revision"]: v["effective"] for v in versions}
        self.assertEqual(effective, {1: False, 3: True})

    def test_batch_order_does_not_matter(self):
        results = []
        closes = []
        for order in ("asc", "desc"):
            db = os.path.join(self._tmp.name, f"order_{order}.db")
            init_workspace(db, base_config())
            records = [
                transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10),
                transfer("T-1", 2, "A", "2026-01-04", "B", "2026-01-05", 20),
            ]
            if order == "desc":
                records.reverse()
            results.append(import_batch(db, batch(*records)))
            rows = read_close(db)["rows"]
            closes.append([(r["location"], r["biz_date"], r["quantity"]) for r in rows])
        self.assertEqual(closes[0], closes[1])
        for result in results:
            doc = result["documents"][0]
            self.assertEqual(doc["new_effective_revision"], 2)
            self.assertEqual(result["versions_inserted"], 2)

    def test_void_removes_all_legs(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        result = import_batch(self.db, batch(void("T-1", 2)))
        self.assertEqual(len(result["changed_rows"]), 10)
        rows = self.close_map()
        for day in range(1, 11):
            self.assertEqual(rows[("A", f"2026-01-{day:02d}")][0], 100)
        for day in range(1, 6):
            self.assertEqual(rows[("B", f"2026-01-{day:02d}")][0], 0)

    def test_void_first_version_is_harmless_noop(self):
        self.init_base()
        result = import_batch(self.db, batch(void("T-1", 1)))
        self.assertEqual(result["changed_rows"], [])

    def test_legs_before_first_anchor_and_on_anchor_day_are_ignored(self):
        self.init_base()
        # 发货日早于 A 首锚点：A 端不影响日结；B 端正常入账
        result = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2025-12-31", "B", "2026-01-09", 5)))
        self.assertEqual(
            [(c["location"], c["biz_date"]) for c in result["changed_rows"]],
            [("B", "2026-01-09"), ("B", "2026-01-10")])
        # 锚点当天的腿不影响日结（盘点实数已含当日流动）
        result = import_batch(self.db, batch(
            transfer("T-2", 1, "B", "2026-01-06", "A", "2026-01-08", 3)))
        self.assertEqual(
            [(c["location"], c["biz_date"], c["new_quantity"]) for c in result["changed_rows"]],
            [("A", "2026-01-08", 103), ("A", "2026-01-09", 103), ("A", "2026-01-10", 103)])
        self.assertEqual(self.qty("B", "2026-01-06"), 7)
        self.assertEqual(self.qty("B", "2026-01-07"), 7)

    def test_negative_book_balance_allowed(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "B", "2026-01-02", "A", "2026-01-02", 50)))
        self.assertEqual(self.qty("B", "2026-01-02"), -50)
        self.assertEqual(self.qty("B", "2026-01-05"), -50)
        self.assertEqual(self.qty("B", "2026-01-06"), 7)  # 锚点重置
        self.assertEqual(self.qty("A", "2026-01-02"), 150)

    def test_quantity_only_correction(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-03", 10)))
        result = import_batch(self.db, batch(
            transfer("T-1", 2, "A", "2026-01-03", "B", "2026-01-03", 25)))
        changed = {(c["location"], c["biz_date"]): (c["old_quantity"], c["new_quantity"])
                   for c in result["changed_rows"]}
        self.assertEqual(changed[("A", "2026-01-03")], (90, 75))
        self.assertEqual(changed[("B", "2026-01-03")], (10, 25))

    def test_same_content_new_revision_is_net_zero_no_rewrite(self):
        """更高修订号内容完全相同：生效切换但净值未变，不报也不重写。"""
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        result = import_batch(self.db, batch(
            transfer("T-1", 2, "A", "2026-01-03", "B", "2026-01-04", 10)))
        self.assertTrue(result["documents"][0]["effective_changed"])
        self.assertEqual(result["scanned_ranges"], [])
        self.assertEqual(result["changed_rows"], [])

    def test_multiple_docs_in_one_batch_are_atomic(self):
        self.init_base()
        result = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-03", 10),
            transfer("T-2", 1, "B", "2026-01-07", "A", "2026-01-08", 4),
        ))
        self.assertEqual(result["versions_inserted"], 2)
        self.assertEqual(self.qty("A", "2026-01-03"), 90)
        self.assertEqual(self.qty("B", "2026-01-03"), 10)
        self.assertEqual(self.qty("B", "2026-01-07"), 3)   # 锚点 7 重置后 - 4
        self.assertEqual(self.qty("A", "2026-01-08"), 94)   # 90 + 4

    def test_other_end_without_balance_row_does_not_block_this_side(self):
        """另一端当天还没有日结行（首锚点未到），不影响本端记账。"""
        config = base_config()
        config["anchors"].append(
            {"location": "C", "biz_date": "2026-01-08", "round": "R1",
             "status": "accepted", "exact_total": 30})
        init_workspace(self.db, config)
        result = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "C", "2026-01-05", 6)))
        # C 的收货腿在其首锚点之前 → 不影响 C；A 端照常
        self.assertEqual(self.qty("A", "2026-01-03"), 94)
        self.assertEqual(self.qty("C", "2026-01-08"), 30)
        self.assertFalse(any(c["location"] == "C" for c in result["changed_rows"]))


class RecordValidationTests(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.init_base()

    def assert_invalid(self, record):
        with self.assertRaises(InvalidRecordError, msg=json.dumps(record)):
            import_batch(self.db, batch(record))

    def test_invalid_records(self):
        bad = [
            {"doc_no": "T", "revision": 1, "type": "transfer"},                      # 缺字段
            {"doc_no": "", "revision": 1, "type": "void"},                           # 空单号
            {"doc_no": "T", "revision": 0, "type": "void"},                          # 修订号非正
            {"doc_no": "T", "revision": -2, "type": "void"},
            {"doc_no": "T", "revision": "2", "type": "void"},                        # 修订号非整数
            {"doc_no": "T", "revision": True, "type": "void"},                       # bool
            {"doc_no": "T", "revision": 1, "type": "cancel"},                        # 未知类型
            void("T", 1) | {"quantity": 3},                                          # void 带字段
            transfer("T", 1, "A", "2026-01-03", "A", "2026-01-03", 1),               # 同库位
            transfer("T", 1, "A", "2026-01-03", "B", "2026-01-03", 0),               # 件数非正
            transfer("T", 1, "A", "2026-01-03", "B", "2026-01-03", -5),
            transfer("T", 1, "A", "2026-01-03", "B", "2026-01-03", 1.5),             # 非整数件数
            transfer("T", 1, "A", "2026-01-05", "B", "2026-01-04", 1),               # 发货日晚于收货日
            transfer("T", 1, "A", "2026-01-09", "B", "2026-01-11", 1),               # 超过关账末日
            transfer("T", 1, "A", "2026-01-03", "B", "2026-01-04", 1) | {"note": "x"},  # 未知字段
            transfer("T", 1, "A", "2026-01-03", "B", "2026-1-4", 1),                 # 日期格式
            transfer("T", 1, "A", "2026-02-29", "B", "2026-03-01", 1),               # 无效日历日
            transfer("T", 1, "NOPE", "2026-01-03", "B", "2026-01-04", 1),            # 未知库位
        ]
        for record in bad:
            self.assert_invalid(record)
        # 全部失败，库内无任何版本
        self.assertEqual(list_versions(self.db)["versions"], [])

    def test_same_day_ship_and_recv_allowed(self):
        result = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-03", 4)))
        self.assertEqual(self.qty("A", "2026-01-03"), 96)
        self.assertEqual(self.qty("B", "2026-01-03"), 4)
        self.assertEqual(len(result["changed_rows"]), 11)  # A 8 行 + B 3 行（01-03..05）


class ReadAndAuditTests(WorkspaceTestCase):
    def test_close_filters(self):
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        rows = read_close(self.db, location="B", date_from="2026-01-04",
                          date_to="2026-01-06")["rows"]
        self.assertEqual([(r["biz_date"], r["quantity"]) for r in rows],
                         [("2026-01-04", 10), ("2026-01-05", 10), ("2026-01-06", 7)])

    def test_batches_and_batch_detail(self):
        self.init_base()
        r1 = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        r2 = import_batch(self.db, batch(void("T-1", 2)))
        batches = list_batches(self.db)["batches"]
        self.assertEqual([b["batch_id"] for b in batches], [r1["batch_id"], r2["batch_id"]])
        self.assertEqual([b["changed_rows"] for b in batches], [10, 10])
        detail = get_batch(self.db, r2["batch_id"])
        self.assertEqual(detail["documents"][0]["new_effective_revision"], 2)
        self.assertEqual(len(detail["changed_rows"]), 10)
        with self.assertRaises(WorkspaceStateError):
            get_batch(self.db, 999)

    def test_persistence_across_reopen(self):
        """每个调用都重新打开 SQLite 文件：状态在关闭重开后完整保留。"""
        self.init_base()
        import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        first = self.close_map()
        # 再次打开同一文件继续导入
        import_batch(self.db, batch(
            transfer("T-1", 2, "A", "2026-01-03", "B", "2026-01-07", 10)))
        self.assertEqual(self.qty("B", "2026-01-07"), 17)
        self.assertEqual(self.qty("B", "2026-01-04"), 0)
        self.assertNotEqual(self.close_map(), first)


class CloseAtBatchTests(WorkspaceTestCase):
    """按批次还原历史日结：当时件数、当前件数与差值（delta = 当前 − 当时）。"""

    def setup_three_batches(self):
        """批1：T-1 A→B 10（01-03 发货 / 01-04 收货）；
        批2：T-2 B→A 4（01-07 发货 / 01-08 收货）；
        批3：迟到更正 T-1 rev2，发货日 01-03 → 01-02（更早业务日）。"""
        self.init_base()
        r1 = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        r2 = import_batch(self.db, batch(
            transfer("T-2", 1, "B", "2026-01-07", "A", "2026-01-08", 4)))
        r3 = import_batch(self.db, batch(
            transfer("T-1", 2, "A", "2026-01-02", "B", "2026-01-04", 10)))
        return r1["batch_id"], r2["batch_id"], r3["batch_id"]

    def then_map(self, batch_id, **filters):
        rows = read_close_at_batch(self.db, batch_id, **filters)["rows"]
        return {(r["location"], r["biz_date"]): r for r in rows}

    def test_batch_view_reflects_state_right_after_that_batch(self):
        b1, _, _ = self.setup_three_batches()
        rows = self.then_map(b1)
        self.assertEqual(len(rows), 20)
        # 批1 刚完成时的完整日结：A 01-03 起 90；B 01-04、01-05 为 10，锚点日 7
        expected_then = {}
        for d in range(1, 11):
            day = f"2026-01-{d:02d}"
            expected_then[("A", day)] = 100 if d <= 2 else 90
            expected_then[("B", day)] = 0 if d <= 3 else (10 if d <= 5 else 7)
        self.assertEqual({k: r["quantity_then"] for k, r in rows.items()}, expected_then)
        # 与当前日结对照：批2 的 B→A 与批3 的迟到更正在批1 时都不存在
        self.assertEqual(
            [(r["quantity_then"], r["quantity_now"], r["delta"])
             for r in (rows[("A", "2026-01-02")], rows[("A", "2026-01-08")],
                       rows[("B", "2026-01-07")], rows[("B", "2026-01-04")])],
            [(100, 90, -10), (90, 94, 4), (7, 3, -4), (10, 10, 0)])
        # 锚点日永不被改写：当时与当前一致
        self.assertEqual(rows[("B", "2026-01-06")]["quantity_then"], 7)
        self.assertEqual(rows[("B", "2026-01-06")]["delta"], 0)

    def test_late_correction_does_not_rewrite_earlier_batch_view(self):
        _, b2, _ = self.setup_three_batches()
        rows = self.then_map(b2)
        # 批3 把 T-1 发货日改到 01-02：当前日结 A 01-02 = 90，
        # 但批2 的历史视图仍还原当时展示的 100，可解释当时的出库/补货决定
        row = rows[("A", "2026-01-02")]
        self.assertEqual((row["quantity_then"], row["quantity_now"], row["delta"]),
                         (100, 90, -10))
        changed = sorted(k for k, r in rows.items() if r["delta"] != 0)
        self.assertEqual(changed, [("A", "2026-01-02")])
        # 当前日结仍是重述后的最新结果
        self.assertEqual(self.qty("A", "2026-01-02"), 90)

    def test_latest_batch_view_matches_current(self):
        _, _, b3 = self.setup_three_batches()
        out = read_close_at_batch(self.db, b3)
        self.assertEqual(out["batch_id"], b3)
        self.assertTrue(all(r["delta"] == 0 for r in out["rows"]))
        self.assertEqual(
            [(r["location"], r["biz_date"], r["quantity_then"]) for r in out["rows"]],
            [(r["location"], r["biz_date"], r["quantity"])
             for r in read_close(self.db)["rows"]])

    def test_filters_and_ordering(self):
        b1, _, _ = self.setup_three_batches()
        out = read_close_at_batch(self.db, b1, location="B",
                                  date_from="2026-01-03", date_to="2026-01-06")
        self.assertEqual(
            [(r["biz_date"], r["quantity_then"], r["quantity_now"]) for r in out["rows"]],
            [("2026-01-03", 0, 0), ("2026-01-04", 10, 10),
             ("2026-01-05", 10, 10), ("2026-01-06", 7, 7)])
        # 全量结果按库位、业务日排序
        keys = [(r["location"], r["biz_date"])
                for r in read_close_at_batch(self.db, b1)["rows"]]
        self.assertEqual(keys, sorted(keys))

    def test_unknown_batch_rejected(self):
        self.init_base()
        with self.assertRaises(WorkspaceStateError):  # 还没有任何批次
            read_close_at_batch(self.db, 1)
        r1 = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        with self.assertRaises(WorkspaceStateError):
            read_close_at_batch(self.db, r1["batch_id"] + 100)
        # 批次号必须为正整数
        for bad in (0, -1, "1", 1.5, True, None):
            with self.assertRaises(InvalidRecordError, msg=repr(bad)):
                read_close_at_batch(self.db, bad)

    def test_invalid_date_range_rejected(self):
        b1, _, _ = self.setup_three_batches()
        with self.assertRaises(InvalidRecordError):  # from 晚于 to
            read_close_at_batch(self.db, b1,
                                date_from="2026-01-06", date_to="2026-01-04")
        with self.assertRaises(InvalidRecordError):  # 日期格式
            read_close_at_batch(self.db, b1, date_from="2026-1-4")
        with self.assertRaises(InvalidRecordError):  # 无效日历日
            read_close_at_batch(self.db, b1, date_to="2026-02-30")

    def test_unknown_location_rejected(self):
        b1, _, _ = self.setup_three_batches()
        with self.assertRaises(WorkspaceStateError):
            read_close_at_batch(self.db, b1, location="NOPE")
        # 只有非 accepted 锚点的库位也不是已知库位
        config = base_config()
        config["anchors"].append(
            {"location": "C", "biz_date": "2026-01-02", "round": "R1",
             "status": "ambiguous", "exact_total": 5})
        db2 = os.path.join(self._tmp.name, "ws2.db")
        init_workspace(db2, config)
        import_batch(db2, batch(transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        with self.assertRaises(WorkspaceStateError):
            read_close_at_batch(db2, 1, location="C")

    def test_history_view_stable_across_later_batches(self):
        """后续批次（含撤销与再更正）不改写较早批次的历史视图。"""
        b1, b2, _ = self.setup_three_batches()
        then_b1 = {k: r["quantity_then"] for k, r in self.then_map(b1).items()}
        then_b2 = {k: r["quantity_then"] for k, r in self.then_map(b2).items()}
        import_batch(self.db, batch(void("T-2", 2)))
        import_batch(self.db, batch(
            transfer("T-1", 3, "A", "2026-01-05", "B", "2026-01-05", 1)))
        # 每次读取都重新打开工作区文件：历史视图在关闭重开后仍可读取且不变
        self.assertEqual(
            {k: r["quantity_then"] for k, r in self.then_map(b1).items()}, then_b1)
        self.assertEqual(
            {k: r["quantity_then"] for k, r in self.then_map(b2).items()}, then_b2)
        # 当前日结已随新批次变化（T-1 rev3 整单替换为 01-05 发货 1 件）
        self.assertEqual(self.qty("A", "2026-01-05"), 99)

    def test_idempotent_resend_batch_view(self):
        """幂等重送产生的空改动批也是已成功批次：其历史视图 = 当前日结。"""
        _, _, b3 = self.setup_three_batches()
        resend = import_batch(self.db, batch(
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)))
        self.assertEqual(resend["versions_inserted"], 0)
        rows = read_close_at_batch(self.db, resend["batch_id"])["rows"]
        self.assertTrue(all(r["delta"] == 0 for r in rows))
        # 与上一批之间没有任何改动：两批的历史视图一致
        self.assertEqual(
            {k: r["quantity_then"] for k, r in self.then_map(resend["batch_id"]).items()},
            {k: r["quantity_then"] for k, r in self.then_map(b3).items()})


class CliTests(WorkspaceTestCase):
    def run_cli(self, *argv, stdin_text=None):
        old_stdin = sys.stdin
        if stdin_text is not None:
            sys.stdin = io.StringIO(stdin_text)
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = cli_main(list(argv))
        finally:
            sys.stdin = old_stdin
        return code, json.loads(out.getvalue())

    def test_full_cli_flow(self):
        config_path = os.path.join(self._tmp.name, "init.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(base_config(), fh)
        code, out = self.run_cli("init", self.db, "--config", config_path)
        self.assertEqual((code, out["status"]), (0, "ok"))

        code, out = self.run_cli("import", self.db, "--batch", "-", stdin_text=json.dumps(
            batch(transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10))))
        self.assertEqual(code, 0)
        self.assertEqual(len(out["changed_rows"]), 10)

        code, out = self.run_cli("close", self.db, "--location", "A",
                                 "--from", "2026-01-03", "--to", "2026-01-03")
        self.assertEqual(out["rows"][0]["quantity"], 90)

        code, out = self.run_cli("versions", self.db, "--doc", "T-1")
        self.assertEqual(len(out["versions"]), 1)
        self.assertTrue(out["versions"][0]["effective"])

        code, out = self.run_cli("batches", self.db)
        self.assertEqual(len(out["batches"]), 1)

        code, out = self.run_cli("batch", self.db, "--id", "1")
        self.assertEqual(len(out["changed_rows"]), 10)

        code, out = self.run_cli("anchors", self.db)
        self.assertEqual(len(out["anchors"]), 3)

    def test_cli_error_exit_code(self):
        self.init_base()
        code, out = self.run_cli("import", self.db, "--batch", "-",
                                 stdin_text=json.dumps(batch(void("T", 0))))
        self.assertEqual(code, 2)
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["error_type"], "InvalidRecordError")

    def test_cli_close_at_flow(self):
        config_path = os.path.join(self._tmp.name, "init.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(base_config(), fh)
        code, _ = self.run_cli("init", self.db, "--config", config_path)
        self.assertEqual(code, 0)
        # 批1：T-1 A→B 10（01-03 发货）；批2：迟到更正，发货日改为 01-02
        for payload in (
            batch(transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-04", 10)),
            batch(transfer("T-1", 2, "A", "2026-01-02", "B", "2026-01-04", 10)),
        ):
            code, _ = self.run_cli("import", self.db, "--batch", "-",
                                   stdin_text=json.dumps(payload))
            self.assertEqual(code, 0)

        code, out = self.run_cli("close-at", self.db, "--batch", "1")
        self.assertEqual((code, out["batch_id"]), (0, 1))
        row = [r for r in out["rows"]
               if r["location"] == "A" and r["biz_date"] == "2026-01-02"][0]
        self.assertEqual((row["quantity_then"], row["quantity_now"], row["delta"]),
                         (100, 90, -10))

        code, out = self.run_cli("close-at", self.db, "--batch", "1",
                                 "--location", "B", "--from", "2026-01-04",
                                 "--to", "2026-01-04")
        self.assertEqual(code, 0)
        self.assertEqual([(r["biz_date"], r["quantity_then"], r["quantity_now"])
                          for r in out["rows"]], [("2026-01-04", 10, 10)])

        # 未知批次 / 无效日期范围 / 不存在的库位：明确报错，退出码 2
        code, out = self.run_cli("close-at", self.db, "--batch", "99")
        self.assertEqual((code, out["status"], out["error_type"]),
                         (2, "error", "WorkspaceStateError"))
        code, out = self.run_cli("close-at", self.db, "--batch", "1",
                                 "--from", "2026-01-06", "--to", "2026-01-04")
        self.assertEqual((code, out["status"], out["error_type"]),
                         (2, "error", "InvalidRecordError"))
        code, out = self.run_cli("close-at", self.db, "--batch", "1",
                                 "--location", "Z")
        self.assertEqual((code, out["status"], out["error_type"]),
                         (2, "error", "WorkspaceStateError"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
