"""只读调拨追溯报告（trace）的测试。

运行方式（隔离模式，避免从工作目录加载到同名模块）::

    python3 -I tests/test_inventory_trace.py
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inventory_close import (  # noqa: E402
    InvalidRecordError,
    WorkspaceStateError,
    import_batch,
    init_workspace,
    list_anchors,
    list_versions,
    read_close,
    trace_transfers,
)
from inventory_close.cli import main as cli_main  # noqa: E402


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


def entry(doc_no, side, rank, revision=1):
    return {"doc_no": doc_no, "revision": revision, "side": side, "rank": rank}


def base_config(*anchors, closing_end="2026-01-12"):
    return {
        "item": "SKU-0001",
        "closing_end": closing_end,
        "anchors": [
            {"location": loc, "biz_date": day, "round": round_id,
             "status": "accepted", "exact_total": total}
            for loc, day, round_id, total in anchors
        ],
    }


def three_locations():
    return base_config(
        ("A", "2026-01-01", "R1", 10),
        ("B", "2026-01-01", "R1", 0),
        ("C", "2026-01-01", "R1", 0),
    )


class TraceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "ws.db")

    def make(self, config, *records, manifest_ranks=None):
        init_workspace(self.db, config)
        if records:
            import_batch(self.db, batch(*records))
        self.records = list(records)
        self.manifest_ranks = manifest_ranks or {}

    def auto_manifest(self, cutoff):
        """按业务日编顺位：同日先发后收，当日顺位全仓唯一。"""
        by_day = {}
        for rec in self.records:
            if rec["type"] != "transfer":
                continue
            doc, rev = rec["doc_no"], rec["revision"]
            if rec["from_date"] <= cutoff:
                by_day.setdefault(rec["from_date"], []).append(
                    (doc, rev, "ship", rec["from_location"]))
            if rec["to_date"] <= cutoff:
                by_day.setdefault(rec["to_date"], []).append(
                    (doc, rev, "recv", rec["to_location"]))
        entries = []
        for day in sorted(by_day):
            legs = sorted(by_day[day],
                          key=lambda x: (0 if x[2] == "ship" else 1, x[0], x[1]))
            for rank, (doc, rev, side, _loc) in enumerate(legs, 1):
                entries.append(entry(doc, side, rank, rev))
        return entries

    def trace(self, location="A", date="2026-01-01", cutoff="2026-01-10",
              manifest=None):
        if manifest is None:
            manifest = self.auto_manifest(cutoff)
        return trace_transfers(self.db, location, date, cutoff, manifest)

    @staticmethod
    def by_doc(result):
        return {(s["doc_no"], s["revision"]): s for s in result["shipments"]}

    @staticmethod
    def source_map(shipment):
        out = {}
        for src in shipment["sources"]:
            if src["source"] == "anchor":
                key = ("anchor", src["location"], src["biz_date"], src["round"])
            else:
                key = ("receipt", src["doc_no"], src["revision"],
                       src["location"], src["biz_date"])
            out[key] = src["quantity"]
        return out


class BasicTraceTests(TraceTestCase):
    def test_returns_start_quantity_and_attributes_each_shipment(self):
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 6),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 2},
        )
        result = self.trace(cutoff="2026-01-10")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["start"], {
            "location": "A", "biz_date": "2026-01-01",
            "round": "R1", "quantity": 10,
        })
        self.assertEqual(len(result["shipments"]), 1)
        shipment = result["shipments"][0]
        self.assertEqual(
            (shipment["doc_no"], shipment["revision"],
             shipment["from_location"], shipment["from_date"],
             shipment["to_location"], shipment["to_date"],
             shipment["origin_quantity"]),
            ("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 6),
        )
        self.assertEqual(self.source_map(shipment), {
            ("anchor", "A", "2026-01-01", "R1"): 6,
        })
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 4},
            {"location": "B", "quantity": 6},
        ])
        self.assertEqual(result["in_flight"], [])
        self.assertEqual(result["stopped_at_anchor"], [])
        self.assertEqual(result["totals"], {
            "start_quantity": 10,
            "remaining": 10,
            "in_flight": 0,
            "stopped_at_anchor": 0,
            "destination_total": 10,
            "balanced": True,
        })

    def test_second_hop_attributes_to_receipt_not_original_anchor(self):
        """经过多次调拨只归到发货库位的最近一次入货，不重复计入去向。"""
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 6),
            transfer("T-2", 1, "B", "2026-01-03", "C", "2026-01-04", 5),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 1,
                ("T-2", "ship"): 1, ("T-2", "recv"): 1,
            },
        )
        result = self.trace(cutoff="2026-01-10")
        shipments = self.by_doc(result)
        self.assertEqual(self.source_map(shipments[("T-2", 1)]), {
            ("receipt", "T-1", 1, "B", "2026-01-02"): 5,
        })
        # 去向只在 C 留 5、B 留 1；不会因为 T-2 是“A 的货”再算一遍。
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 4},
            {"location": "B", "quantity": 1},
            {"location": "C", "quantity": 5},
        ])
        self.assertTrue(result["totals"]["balanced"])

    def test_zero_contribution_shipments_are_omitted(self):
        """发货全是别的存量时不列出（含零贡献不列）。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 2),
            ("B", "2026-01-01", "R1", 5),
            ("C", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            transfer("T-1", 1, "B", "2026-01-02", "C", "2026-01-02", 5),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 2},
        )
        result = self.trace(cutoff="2026-01-10")
        self.assertEqual(result["shipments"], [])
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 2},
        ])
        self.assertTrue(result["totals"]["balanced"])


class FifoTests(TraceTestCase):
    def test_in_batch_order_survives_receipt(self):
        """收货里先到 4 件别的存量、后 2 件起点；目的地先发 4 件不带起点货。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 2),
            ("B", "2026-01-01", "R1", 4),
            ("C", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 2),
            transfer("T-2", 1, "B", "2026-01-03", "C", "2026-01-04", 4),
            transfer("T-3", 1, "B", "2026-01-03", "C", "2026-01-04", 2),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 2,
                ("T-2", "ship"): 1, ("T-3", "ship"): 2,
                ("T-2", "recv"): 1, ("T-3", "recv"): 2,
            },
        )
        result = self.trace(cutoff="2026-01-10")
        shipments = self.by_doc(result)
        self.assertNotIn(("T-2", 1), shipments)  # 前 4 件不带起点货
        self.assertEqual(shipments[("T-3", 1)]["origin_quantity"], 2)
        self.assertEqual(self.source_map(shipments[("T-3", 1)]), {
            ("receipt", "T-1", 1, "B", "2026-01-02"): 2,
        })
        self.assertEqual(result["remaining"], [
            {"location": "C", "quantity": 2},
        ])
        self.assertTrue(result["totals"]["balanced"])

    def test_shipment_can_take_from_several_source_records(self):
        """一次发货跨批取货，来源按入货记录分别列出且顺序保留。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 4),
            ("B", "2026-01-01", "R1", 2),
            ("C", "2026-01-01", "R1", 0),
            ("D", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            # A 的起点货 01-02 先到 C（2 件），01-03 再到 2 件
            transfer("T-1", 1, "A", "2026-01-02", "C", "2026-01-02", 2),
            transfer("T-2", 1, "A", "2026-01-03", "C", "2026-01-03", 2),
            # C 01-04 一次发 4 件：先 2 件来自 T-1，再 2 件来自 T-2
            transfer("T-3", 1, "C", "2026-01-04", "D", "2026-01-04", 4),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 2,
                ("T-2", "ship"): 1, ("T-2", "recv"): 2,
                ("T-3", "ship"): 1, ("T-3", "recv"): 2,
            },
        )
        result = self.trace(cutoff="2026-01-10")
        shipment = self.by_doc(result)[("T-3", 1)]
        self.assertEqual(shipment["origin_quantity"], 4)
        self.assertEqual(self.source_map(shipment), {
            ("receipt", "T-1", 1, "C", "2026-01-02"): 2,
            ("receipt", "T-2", 1, "C", "2026-01-03"): 2,
        })
        self.assertEqual([s["doc_no"] for s in shipment["sources"]],
                         ["T-1", "T-2"])
        self.assertTrue(result["totals"]["balanced"])

    def test_mixed_receipt_batch_keeps_its_internal_order(self):
        """一笔收货里先 4 件别的、后 2 件起点；目的地先发 4 件就不带起点货。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 2),
            ("B", "2026-01-01", "R1", 4),
            ("C", "2026-01-01", "R1", 0),
            ("D", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            # B 原有 4 件别的存量，随后收到 2 件起点货；B 一次发 6 件到 C，
            # 这一批内部顺序是 [别的 4 件, 起点 2 件]。
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 2),
            transfer("T-2", 1, "B", "2026-01-03", "C", "2026-01-03", 6),
            # C 紧接着先发 4 件（应为批内前 4 件别的货），再发 2 件（起点货）。
            transfer("T-3", 1, "C", "2026-01-04", "D", "2026-01-04", 4),
            transfer("T-4", 1, "C", "2026-01-05", "D", "2026-01-05", 2),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 2,
                ("T-2", "ship"): 1, ("T-2", "recv"): 2,
                ("T-3", "ship"): 1, ("T-3", "recv"): 2,
                ("T-4", "ship"): 1, ("T-4", "recv"): 2,
            },
        )
        result = self.trace(cutoff="2026-01-10")
        shipments = self.by_doc(result)
        self.assertNotIn(("T-3", 1), shipments)
        self.assertEqual(shipments[("T-4", 1)]["origin_quantity"], 2)
        self.assertEqual(self.source_map(shipments[("T-4", 1)]), {
            ("receipt", "T-2", 1, "C", "2026-01-03"): 2,
        })
        self.assertEqual(result["remaining"], [
            {"location": "D", "quantity": 2},
        ])
        self.assertTrue(result["totals"]["balanced"])

    def test_earliest_receipt_is_used_before_later_stock(self):
        """同一库位两个库位来源：按“最早进入”而不是按清单行序取用。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 6),
            ("B", "2026-01-01", "R1", 0),
            ("C", "2026-01-01", "R1", 0),
            ("D", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            # 01-02 先到 2 件起点货，01-04 再到 5 件别的库位的货
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 2),
            transfer("T-2", 1, "C", "2026-01-03", "B", "2026-01-04", 5),
            transfer("T-3", 1, "B", "2026-01-05", "D", "2026-01-06", 4),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 1,
                ("T-2", "ship"): 1, ("T-2", "recv"): 1,
                ("T-3", "ship"): 1, ("T-3", "recv"): 1,
            },
        )
        result = self.trace(cutoff="2026-01-10")
        shipments = self.by_doc(result)
        # B 在 01-05 先取 01-02 到的 2 件起点货，再取 01-04 到的 2 件别的货
        self.assertEqual(shipments[("T-3", 1)]["origin_quantity"], 2)
        self.assertEqual(self.source_map(shipments[("T-3", 1)]), {
            ("receipt", "T-1", 1, "B", "2026-01-02"): 2,
        })
        # B 被取剩的 3 件都是别的库位来的货，不再列（零贡献不列）。
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 4},
            {"location": "D", "quantity": 2},
        ])
        self.assertTrue(result["totals"]["balanced"])

    def test_shortfall_is_unknown_and_follows_actual_stock(self):
        """库位不足时缺多少补多少来源不明；补货排在实际取到的货之后。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 2),
            ("B", "2026-01-01", "R1", 0),
            ("C", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            # A 只有 2 件，却发 5 件：2 件起点 + 3 件来源不明
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 5),
            # B 收到的批次顺序是 2 件起点、3 件来源不明；B 再发 3 件
            transfer("T-2", 1, "B", "2026-01-03", "C", "2026-01-03", 3),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 1,
                ("T-2", "ship"): 1, ("T-2", "recv"): 2,
            },
        )
        result = self.trace(cutoff="2026-01-10")
        shipments = self.by_doc(result)
        self.assertEqual(shipments[("T-1", 1)]["origin_quantity"], 2)
        self.assertEqual(shipments[("T-1", 1)]["quantity"], 5)
        # 先取到的是 2 件起点货，随后才是 1 件来源不明
        self.assertEqual(shipments[("T-2", 1)]["origin_quantity"], 2)
        self.assertEqual(result["remaining"], [
            {"location": "C", "quantity": 2},
        ])
        self.assertEqual(result["totals"]["destination_total"], 2)
        self.assertTrue(result["totals"]["balanced"])

    def test_large_quantities_do_not_expand(self):
        """大件数与重复拆并只按批量片段推进，不产生逐件状态。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 10 ** 9),
            ("B", "2026-01-01", "R1", 0),
            ("C", "2026-01-01", "R1", 0),
        )
        records = [
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 10 ** 9),
        ]
        manifest = {("T-1", "ship"): 1, ("T-1", "recv"): 1}
        for i in range(2, 40):
            day = f"2026-01-{(i % 9) + 2:02d}"
            records.append(transfer(f"T-{i}", 1, "B" if i % 2 else "C",
                                    day, "C" if i % 2 else "B", day, 10 ** 6))
            manifest[(f"T-{i}", "ship")] = 1 + i
            manifest[(f"T-{i}", "recv")] = 1 + i
        self.make(config, *records, manifest_ranks=manifest)
        result = self.trace(cutoff="2026-01-10")
        self.assertTrue(result["totals"]["balanced"])
        self.assertEqual(result["totals"]["start_quantity"], 10 ** 9)


class AnchorTests(TraceTestCase):
    def test_later_anchor_stops_trace_and_restarts_stock(self):
        config = base_config(
            ("A", "2026-01-01", "R1", 10),
            ("A", "2026-01-06", "R2", 7),
            ("B", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 4),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 2},
        )
        result = self.trace(cutoff="2026-01-10")
        self.assertEqual(result["shipments"][0]["origin_quantity"], 4)
        self.assertEqual(result["remaining"], [
            {"location": "B", "quantity": 4},
        ])
        # 01-06 盘点前 A 还剩 6 件起点货，被盘点中止追溯
        self.assertEqual(result["stopped_at_anchor"], [{
            "location": "A", "biz_date": "2026-01-06",
            "round": "R2", "quantity": 6,
        }])
        self.assertEqual(result["totals"]["stopped_at_anchor"], 6)
        self.assertTrue(result["totals"]["balanced"])

    def test_anchor_stops_only_units_still_at_that_location(self):
        """已发或在途的货不受该库位盘点影响。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 10),
            ("A", "2026-01-05", "R2", 3),
            ("B", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            # 01-03 已发 4 件（01-08 才收），01-05 盘点前 A 还剩 6 件
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-08", 4),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 1},
        )
        result = self.trace(cutoff="2026-01-10")
        self.assertEqual(result["stopped_at_anchor"], [{
            "location": "A", "biz_date": "2026-01-05",
            "round": "R2", "quantity": 6,
        }])
        self.assertEqual(result["in_flight"], [])
        self.assertEqual(result["remaining"], [
            {"location": "B", "quantity": 4},
        ])
        self.assertTrue(result["totals"]["balanced"])

    def test_receipts_before_first_anchor_enter_but_anchor_counts_fresh(self):
        """首盘点前不假定开账存量，但已发生的收货照常进入。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 5),
            ("B", "2026-01-05", "R1", 2),
            ("C", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            # B 在首盘点前收到 3 件（其中 2 件来自起点），随后盘点为 2 件
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 3),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 2},
        )
        result = self.trace(cutoff="2026-01-10")
        self.assertEqual(result["shipments"][0]["origin_quantity"], 3)
        # B 首盘点前有 3 件起点货，全部在盘点时中止追溯；盘后按点数 2 件
        # 开启一份不可追溯的新存量。
        self.assertEqual(result["stopped_at_anchor"], [{
            "location": "B", "biz_date": "2026-01-05",
            "round": "R1", "quantity": 3,
        }])
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 2},
        ])
        self.assertEqual(result["totals"]["destination_total"], 5)
        self.assertTrue(result["totals"]["balanced"])

    def test_negative_later_anchor_restarts_with_empty_stock(self):
        """后续盘点存档了负数时，只中止旧起点货，不产生负实物存量。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 3),
            ("A", "2026-01-05", "R2", 0),
            ("B", "2026-01-01", "R1", 0),
        )
        config["anchors"][1]["exact_total"] = -2  # 允许存档，但不是实物
        self.make(config)
        result = self.trace(cutoff="2026-01-10")
        self.assertEqual(result["stopped_at_anchor"], [{
            "location": "A", "biz_date": "2026-01-05",
            "round": "R2", "quantity": 3,
        }])
        self.assertEqual(result["totals"]["destination_total"], 3)
        self.assertTrue(result["totals"]["balanced"])

    def test_negative_start_anchor_is_rejected(self):
        config = base_config(
            ("A", "2026-01-01", "R1", 0),
            ("B", "2026-01-01", "R1", 0),
        )
        config["anchors"][0]["exact_total"] = -1
        self.make(config)
        with self.assertRaises(WorkspaceStateError):
            trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", [])

    def test_shipment_on_start_anchor_day_is_not_from_start_stock(self):
        """盘点发生在当天调拨结束后：起点当天的发货带走的是盘前旧货。"""
        config = base_config(
            ("A", "2026-01-01", "R1", 10),
            ("B", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            transfer("T-1", 1, "A", "2026-01-01", "B", "2026-01-01", 3),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 2},
        )
        result = self.trace(cutoff="2026-01-10")
        self.assertEqual(result["shipments"], [])
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 10},
        ])
        self.assertTrue(result["totals"]["balanced"])

    def test_returning_to_start_location_is_not_a_new_origin(self):
        config = base_config(
            ("A", "2026-01-01", "R1", 6),
            ("B", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 4),
            transfer("T-2", 1, "B", "2026-01-03", "A", "2026-01-04", 3),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 1,
                ("T-2", "ship"): 1, ("T-2", "recv"): 2,
            },
        )
        result = self.trace(cutoff="2026-01-10")
        # 起点件数仍是 6；回来的 3 件是既有起点货，不是新增。
        self.assertEqual(result["start"]["quantity"], 6)
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 5},
            {"location": "B", "quantity": 1},
        ])
        self.assertEqual(result["totals"]["start_quantity"], 6)
        self.assertTrue(result["totals"]["balanced"])

    def test_start_on_later_anchor_ignores_earlier_a_stock(self):
        config = base_config(
            ("A", "2026-01-01", "R1", 10),
            ("A", "2026-01-05", "R2", 4),
            ("B", "2026-01-01", "R1", 0),
        )
        self.make(
            config,
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 3),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 2},
        )
        result = self.trace(date="2026-01-05", cutoff="2026-01-10")
        self.assertEqual(result["start"]["quantity"], 4)
        # 起点之前的 T-1 不携带起点货；01-05 盘点的 4 件开启新存量。
        self.assertEqual(result["shipments"], [])
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 4},
        ])
        self.assertTrue(result["totals"]["balanced"])


class InFlightTests(TraceTestCase):
    def test_shipment_after_cutoff_receipt_is_in_flight(self):
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-08", 5),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 1},
        )
        result = self.trace(cutoff="2026-01-05")
        self.assertEqual(result["shipments"][0]["origin_quantity"], 5)
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 5},
        ])
        self.assertEqual(result["in_flight"], [{
            "doc_no": "T-1", "revision": 1,
            "from_location": "A", "from_date": "2026-01-03",
            "to_location": "B", "to_date": "2026-01-08",
            "quantity": 5,
        }])
        self.assertEqual(result["totals"]["in_flight"], 5)
        self.assertTrue(result["totals"]["balanced"])
        # 清单只需覆盖截至日前的发货腿，收货腿不在范围内。
        result2 = self.trace(cutoff="2026-01-05",
                             manifest=[entry("T-1", "ship", 1)])
        self.assertEqual(result2["in_flight"], result["in_flight"])
        self.assertTrue(result2["totals"]["balanced"])

    def test_receipt_on_cutoff_day_is_not_in_flight(self):
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-03", "B", "2026-01-05", 5),
            manifest_ranks={("T-1", "ship"): 1, ("T-1", "recv"): 2},
        )
        result = self.trace(cutoff="2026-01-05")
        self.assertEqual(result["in_flight"], [])
        self.assertEqual(result["remaining"], [
            {"location": "A", "quantity": 5},
            {"location": "B", "quantity": 5},
        ])
        self.assertTrue(result["totals"]["balanced"])


class ManifestValidationTests(TraceTestCase):
    def setUp(self):
        super().setUp()
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 2),
            transfer("T-2", 1, "B", "2026-01-03", "C", "2026-01-04", 2),
        )
        self.full = [
            entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
        ]

    def assert_bad(self, manifest, exc=InvalidRecordError):
        with self.assertRaises(exc):
            trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", manifest)

    def test_missing_leg_is_rejected(self):
        self.assert_bad(self.full[:-1])

    def test_duplicate_same_side_is_rejected(self):
        self.assert_bad(self.full + [entry("T-1", "ship", 3)])

    def test_duplicate_day_rank_is_rejected(self):
        # T-1 当天发收，发货与收货必须占用不同的当日顺位
        self.assert_bad([
            entry("T-1", "ship", 1), entry("T-1", "recv", 1),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
        ])

    def test_stale_revision_is_rejected(self):
        import_batch(self.db, batch(
            transfer("T-2", 2, "B", "2026-01-03", "C", "2026-01-04", 2)))
        stale = [
            entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
        ]
        self.assert_bad(stale)

    def test_void_revision_is_rejected(self):
        import_batch(self.db, batch(void("T-2", 2)))
        self.assert_bad([
            entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
        ])

    def test_out_of_range_leg_is_rejected(self):
        # 收货日在 01-04，截至 01-03 时收货腿不在范围内，不能列
        self.assert_bad([
            entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
        ]) if False else None
        with self.assertRaises(InvalidRecordError):
            trace_transfers(self.db, "A", "2026-01-01", "2026-01-03", [
                entry("T-1", "ship", 1), entry("T-1", "recv", 2),
                entry("T-2", "ship", 1), entry("T-2", "recv", 2),
            ])

    def test_unknown_doc_is_rejected(self):
        self.assert_bad(self.full + [entry("NOPE", "ship", 9)])

    def test_non_positive_rank_is_rejected(self):
        self.assert_bad([
            entry("T-1", "ship", 0), entry("T-1", "recv", 2),
            entry("T-2", "ship", 1), entry("T-2", "recv", 3),
        ])

    def test_same_day_ship_must_precede_receipt(self):
        self.assert_bad([
            entry("T-1", "ship", 5), entry("T-1", "recv", 4),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
        ])

    def test_unknown_manifest_field_is_rejected(self):
        bad = [dict(self.full[0], note="x")] + self.full[1:]
        self.assert_bad(bad)

    def test_manifest_may_be_object_or_flat_pairs(self):
        result = trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", {
            "entries": self.full,
        })
        self.assertTrue(result["totals"]["balanced"])
        result = trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", [
            {"doc_no": "T-1", "revision": 1, "ship": 1, "recv": 2},
            {"doc_no": "T-2", "revision": 1, "ship": 1, "recv": 2},
        ])
        self.assertTrue(result["totals"]["balanced"])

    def test_manifest_entries_before_range_are_optional(self):
        """清单只需覆盖截至日前的腿；与起点无关的早期腿仍必须覆盖。"""
        with self.assertRaises(InvalidRecordError):
            trace_transfers(self.db, "A", "2026-01-01", "2026-01-03", [
                entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            ])


class BoundaryTests(TraceTestCase):
    def test_cutoff_beyond_closing_end_is_rejected(self):
        self.make(three_locations())
        with self.assertRaises(InvalidRecordError):
            trace_transfers(self.db, "A", "2026-01-01", "2026-01-13", [])

    def test_cutoff_before_start_is_rejected(self):
        self.make(three_locations())
        with self.assertRaises(InvalidRecordError):
            trace_transfers(self.db, "A", "2026-01-05", "2026-01-04", [])

    def test_missing_start_anchor_is_rejected(self):
        self.make(three_locations())
        with self.assertRaises(WorkspaceStateError):
            trace_transfers(self.db, "A", "2026-01-02", "2026-01-10", [])

    def test_non_accepted_anchor_is_not_a_start(self):
        config = base_config(
            ("A", "2026-01-01", "R1", 10),
            ("B", "2026-01-01", "R1", 0),
        )
        config["anchors"].append({
            "location": "B", "biz_date": "2026-01-03", "round": "R9",
            "status": "pending", "exact_total": 5,
        })
        self.make(config)
        with self.assertRaises(WorkspaceStateError):
            trace_transfers(self.db, "B", "2026-01-03", "2026-01-10", [])

    def test_unknown_location_is_rejected(self):
        self.make(three_locations())
        with self.assertRaises(WorkspaceStateError):
            trace_transfers(self.db, "Z", "2026-01-01", "2026-01-10", [])

    def test_missing_workspace_is_rejected(self):
        with self.assertRaises(WorkspaceStateError):
            trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", [])


class OrderingAndReadOnlyTests(TraceTestCase):
    def build(self):
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 4),
            transfer("T-2", 1, "B", "2026-01-03", "C", "2026-01-04", 2),
            transfer("T-3", 1, "A", "2026-01-03", "C", "2026-01-05", 3),
            manifest_ranks={
                ("T-1", "ship"): 1, ("T-1", "recv"): 1,
                ("T-2", "ship"): 1, ("T-2", "recv"): 1,
                ("T-3", "ship"): 2, ("T-3", "recv"): 2,
            },
        )

    def test_report_order_does_not_follow_manifest_line_order(self):
        self.build()
        manifest = [
            entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
            entry("T-3", "ship", 3), entry("T-3", "recv", 3),
        ]
        first = trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", manifest)
        second = trace_transfers(
            self.db, "A", "2026-01-01", "2026-01-10", list(reversed(manifest)))
        self.assertEqual(first, second)

        def ordered(result):
            return [
                tuple(s["doc_no"] for s in result["shipments"]),
                [r["location"] for r in result["remaining"]],
                [(r["doc_no"], r["revision"]) for r in result["in_flight"]],
                [(r["location"], r["biz_date"]) for r in result["stopped_at_anchor"]],
            ]

        self.assertEqual(ordered(first), ordered(second))

    def test_trace_is_read_only_and_idempotent(self):
        self.build()

        def digest():
            with open(self.db, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()

        close_before = read_close(self.db)["rows"]
        anchors_before = list_anchors(self.db)["anchors"]
        versions_before = list_versions(self.db)["versions"]
        before = digest()
        manifest = [
            entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            entry("T-2", "ship", 1), entry("T-2", "recv", 2),
            entry("T-3", "ship", 3), entry("T-3", "recv", 3),
        ]
        first = trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", manifest)
        second = trace_transfers(self.db, "A", "2026-01-01", "2026-01-10", manifest)
        self.assertEqual(first, second)
        self.assertEqual(digest(), before)
        self.assertEqual(read_close(self.db)["rows"], close_before)
        self.assertEqual(list_anchors(self.db)["anchors"], anchors_before)
        self.assertEqual(list_versions(self.db)["versions"], versions_before)


class CliTests(TraceTestCase):
    def _run(self, argv, stdin_text=None):
        out = io.StringIO()
        old_stdin = sys.stdin
        try:
            if stdin_text is not None:
                sys.stdin = io.StringIO(stdin_text)
            with redirect_stdout(out):
                code = cli_main(argv)
        finally:
            sys.stdin = old_stdin
        return code, json.loads(out.getvalue())

    def test_cli_trace_flow(self):
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 6),
        )
        manifest = {
            "entries": [
                entry("T-1", "ship", 1), entry("T-1", "recv", 2),
            ],
        }
        code, out = self._run(
            ["trace", self.db, "--location", "A", "--date", "2026-01-01",
             "--cutoff", "2026-01-10", "--manifest", "-"],
            stdin_text=json.dumps(manifest),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["shipments"][0]["origin_quantity"], 6)

    def test_cli_manifest_error_exit_code(self):
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 6),
        )
        code, out = self._run(
            ["trace", self.db, "--location", "A", "--date", "2026-01-01",
             "--cutoff", "2026-01-10", "--manifest", "-"],
            stdin_text=json.dumps({"entries": [entry("T-1", "ship", 1)]}),
        )
        self.assertEqual(code, 2)
        self.assertEqual(out["error_type"], "InvalidRecordError")
        self.assertIn("缺少", out["reason"])

    def test_cli_cutoff_error_exit_code(self):
        self.make(three_locations())
        code, out = self._run(
            ["trace", self.db, "--location", "A", "--date", "2026-01-01",
             "--cutoff", "2026-02-01", "--manifest", "-"],
            stdin_text="[]",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out["error_type"], "InvalidRecordError")

    def test_existing_commands_still_work(self):
        self.make(
            three_locations(),
            transfer("T-1", 1, "A", "2026-01-02", "B", "2026-01-02", 6),
        )
        code, out = self._run(["close", self.db, "--location", "A"])
        self.assertEqual(code, 0)
        self.assertTrue(out["rows"])
        code, out = self._run(["versions", self.db])
        self.assertEqual(code, 0)
        self.assertEqual(len(out["versions"]), 1)


if __name__ == "__main__":
    unittest.main()
