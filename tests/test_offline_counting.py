"""手编小分量的离线盘点入仓器测试。

运行方式（隔离模式，避免从工作目录加载到同名模块）::

    python3 -I tests/test_offline_counting.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from offline_counting import (  # noqa: E402
    CoverEvidence,
    InventoryEngine,
    Status,
    TicketConflictError,
    UnknownLabelError,
    DuplicateLabelError,
    UnknownLocationError,
    UnknownWithdrawalRefError,
    InvalidTicketError,
)
from offline_counting.cli import main as cli_main, run as cli_run  # noqa: E402


def make_engine(locations):
    return InventoryEngine(locations)


class ExactCoverTests(unittest.TestCase):
    def test_box_and_piece_registrations_consistent(self):
        """整箱登记与分件登记总量一致：两套完整覆盖，可供数。"""
        engine = make_engine({"L1": ["a", "b", "c"]})
        engine.submit_ticket("abc", "L1", "R1", ["a", "b", "c"], 30)
        engine.submit_ticket("ab", "L1", "R1", ["a", "b"], 20)
        engine.submit_ticket("c", "L1", "R1", ["c"], 10)

        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.ACCEPTED)
        self.assertEqual(result.exact_total, 30)
        comp = result.components[0]
        self.assertEqual(comp.cover_count, 2)  # {abc} 与 {ab,c}
        self.assertIsNotNone(comp.evidence)
        # 整票数量只计一次，绝不按标签摊分：ab=20 覆盖两个标签仍是 20。
        totals = {comp.evidence.total}
        self.assertEqual(totals, {30})

    def test_abc_ab_bc_cover_then_withdraw_abc_no_cover(self):
        """票 abc、ab、bc 都在时可选 abc；撤掉 abc 后标签虽仍在票里，
        但 ab 与 bc 重叠于 b，不存在不重叠覆盖。"""
        engine = make_engine({"L1": ["a", "b", "c"]})
        engine.submit_ticket("abc", "L1", "R1", ["a", "b", "c"], 30)
        engine.submit_ticket("ab", "L1", "R1", ["a", "b"], 20)
        engine.submit_ticket("bc", "L1", "R1", ["b", "c"], 10)

        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.ACCEPTED)
        comp = result.components[0]
        self.assertEqual(comp.cover_count, 1)
        self.assertEqual(comp.evidence.ticket_ids, ("abc",))

        self.assertTrue(engine.withdraw("abc"))
        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.NO_COVER)
        self.assertIsNone(result.exact_total)
        comp = result.components[0]
        self.assertEqual(comp.status, Status.NO_COVER)
        self.assertTrue(comp.obstacles)  # 必须给出可复查障碍
        joined = " ".join(comp.obstacles)
        for label in ("a", "b", "c"):
            self.assertIn(label, joined)

    def test_withdraw_one_piece_ticket_another_full_cover_remains(self):
        """撤掉一个件级票后，完整覆盖仍可能存在（整箱票独立成立）。"""
        engine = make_engine({"L1": ["a", "b", "c"]})
        engine.submit_ticket("ab", "L1", "R1", ["a", "b"], 20)
        engine.submit_ticket("c", "L1", "R1", ["c"], 10)
        engine.submit_ticket("abc", "L1", "R1", ["a", "b", "c"], 30)

        engine.withdraw("c")
        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.ACCEPTED)
        self.assertEqual(result.exact_total, 30)
        self.assertEqual(result.components[0].evidence.ticket_ids, ("abc",))

    def test_multiple_covers_with_inconsistent_totals_rejected(self):
        """多套完整覆盖但总数不一致：不可入仓，给两套可复查票组和数量。"""
        engine = make_engine({"L1": ["a", "b", "c"]})
        engine.submit_ticket("abc", "L1", "R1", ["a", "b", "c"], 31)
        engine.submit_ticket("ab", "L1", "R1", ["a", "b"], 20)
        engine.submit_ticket("c", "L1", "R1", ["c"], 10)

        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.AMBIGUOUS)
        self.assertIsNone(result.exact_total)
        comp = result.components[0]
        self.assertIsNotNone(comp.evidence)
        self.assertIsNotNone(comp.second_evidence)
        totals = {comp.evidence.total, comp.second_evidence.total}
        self.assertEqual(totals, {30, 31})
        id_sets = {
            frozenset(comp.evidence.ticket_ids),
            frozenset(comp.second_evidence.ticket_ids),
        }
        self.assertEqual(id_sets, {frozenset(["abc"]), frozenset(["ab", "c"])})

    def test_component_split_and_location_sum(self):
        """无共同票据的标签自然分成两个分量；库位总量为分量精确件数之和，
        不展开分量间组合。"""
        engine = make_engine({"L1": ["a", "b", "c", "d"]})
        engine.submit_ticket("ab", "L1", "R1", ["a", "b"], 20)
        engine.submit_ticket("cd", "L1", "R1", ["c", "d"], 7)
        engine.submit_ticket("abcd", "L1", "R1", ["a", "b", "c", "d"], 27)

        result = engine.evaluate("L1", "R1")
        # abcd 与两个分件票共享标签，整张图被并查集连成一个分量。
        self.assertEqual(len(result.components), 1)
        self.assertEqual(result.status, Status.ACCEPTED)
        self.assertEqual(result.exact_total, 27)

        engine2 = make_engine({"L2": ["a", "b", "c", "d"]})
        engine2.submit_ticket("ab", "L2", "R1", ["a", "b"], 20)
        engine2.submit_ticket("cd", "L2", "R1", ["c", "d"], 7)
        result2 = engine2.evaluate("L2", "R1")
        self.assertEqual(len(result2.components), 2)  # 互无共同票据
        self.assertEqual(result2.exact_total, 27)
        self.assertEqual(
            [e.total for e in result2.evidence], [20, 7]
        )

    def test_opposite_differences_cannot_be_averaged_across_components(self):
        """两个分量各自歧义且差额相反，也不允许互相平均掉。"""
        engine = make_engine({"L1": ["a", "b", "c", "d"]})
        # 分量一 {a,b}：20 或 30
        engine.submit_ticket("ab1", "L1", "R1", ["a", "b"], 20)
        engine.submit_ticket("ab2", "L1", "R1", ["a", "b"], 30)
        # 分量二 {c,d}：30 或 20（反向差额）
        engine.submit_ticket("cd1", "L1", "R1", ["c", "d"], 30)
        engine.submit_ticket("cd2", "L1", "R1", ["c", "d"], 20)

        result = engine.evaluate("L1", "R1")
        self.assertEqual(len(result.components), 2)
        self.assertEqual(result.status, Status.AMBIGUOUS)
        self.assertIsNone(result.exact_total)  # 绝不能输出平均后的 50/25 之类
        for comp in result.components:
            self.assertEqual(comp.status, Status.AMBIGUOUS)
            self.assertIsNotNone(comp.evidence)
            self.assertIsNotNone(comp.second_evidence)

    def test_one_bad_component_rejects_whole_location(self):
        """一个分量无覆盖、另一个分量可入仓时，库位整体拒绝，
        但正常分量的证据仍完整保留。"""
        engine = make_engine({"L1": ["a", "b", "c"]})
        engine.submit_ticket("ab", "L1", "R1", ["a", "b"], 20)
        # c 没有任何票
        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.NO_COVER)
        statuses = sorted(c.status for c in result.components)
        self.assertEqual(statuses, [Status.ACCEPTED, Status.NO_COVER])
        no_cover = next(c for c in result.components if c.status == Status.NO_COVER)
        self.assertEqual(no_cover.labels, ("c",))
        self.assertIn("无任何有效票据", " ".join(no_cover.obstacles))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine({"L1": ["a", "b"]})

    def test_unknown_label_rejected_no_virtual_labels(self):
        with self.assertRaises(UnknownLabelError):
            self.engine.submit_ticket("T", "L1", "R1", ["a", "ghost"], 1)

    def test_duplicate_label_inside_ticket_rejected(self):
        with self.assertRaises(DuplicateLabelError):
            self.engine.submit_ticket("T", "L1", "R1", ["a", "a"], 1)

    def test_unknown_location(self):
        with self.assertRaises(UnknownLocationError):
            self.engine.submit_ticket("T", "NOWHERE", "R1", ["a"], 1)
        with self.assertRaises(UnknownLocationError):
            self.engine.evaluate("NOWHERE", "R1")

    def test_conflicting_resubmission_vs_idempotent_resubmission(self):
        self.assertTrue(
            self.engine.submit_ticket("T1", "L1", "R1", ["a", "b"], 5)
        )
        # 完全相同重送：幂等
        self.assertFalse(
            self.engine.submit_ticket("T1", "L1", "R1", ["b", "a"], 5)
        )
        # 内容不一致（件数变化）：冲突
        with self.assertRaises(TicketConflictError):
            self.engine.submit_ticket("T1", "L1", "R1", ["a", "b"], 6)
        # 内容不一致（轮次变化）：冲突
        with self.assertRaises(TicketConflictError):
            self.engine.submit_ticket("T1", "L1", "R2", ["a", "b"], 5)
        # 内容不一致（标签变化）：冲突
        with self.assertRaises(TicketConflictError):
            self.engine.submit_ticket("T1", "L1", "R1", ["a"], 5)

    def test_withdrawal_rules(self):
        self.engine.submit_ticket("T1", "L1", "R1", ["a", "b"], 5)
        with self.assertRaises(UnknownWithdrawalRefError):
            self.engine.withdraw("NOPE")
        self.assertTrue(self.engine.withdraw("T1"))
        self.assertFalse(self.engine.withdraw("T1"))  # 重复撤票幂等
        # 撤票后同内容重送不改变已撤状态
        self.assertFalse(
            self.engine.submit_ticket("T1", "L1", "R1", ["a", "b"], 5)
        )
        self.assertTrue(self.engine.is_withdrawn("T1"))

    def test_quantity_must_be_nonnegative_integer(self):
        for bad in (-1, 1.5, True):
            with self.assertRaises(InvalidTicketError):
                self.engine.submit_ticket(
                    f"T-{bad!r}", "L1", "R1", ["a"], bad
                )
        # 0 件是合法的非负整数
        self.engine.submit_ticket("TZ", "L1", "R1", ["a", "b"], 0)
        result = self.engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.ACCEPTED)
        self.assertEqual(result.exact_total, 0)

    def test_empty_ticket_rejected(self):
        with self.assertRaises(InvalidTicketError):
            self.engine.submit_ticket("T", "L1", "R1", [], 1)


class RoundIsolationTests(unittest.TestCase):
    def test_rounds_never_combined(self):
        engine = make_engine({"L1": ["a", "b"]})
        engine.submit_ticket("a-r1", "L1", "R1", ["a"], 1)
        engine.submit_ticket("b-r2", "L1", "R2", ["b"], 1)

        # 合起来恰好覆盖 a,b，但轮次不同，禁止拼成一次完整盘点
        self.assertEqual(engine.evaluate("L1", "R1").status, Status.NO_COVER)
        self.assertEqual(engine.evaluate("L1", "R2").status, Status.NO_COVER)

        engine.submit_ticket("ab-r1", "L1", "R1", ["a", "b"], 2)
        r1 = engine.evaluate("L1", "R1")
        self.assertEqual(r1.status, Status.ACCEPTED)
        self.assertEqual(r1.exact_total, 2)
        # R2 仍然只有半张票，不受 R1 影响
        self.assertEqual(engine.evaluate("L1", "R2").status, Status.NO_COVER)


class LimitTests(unittest.TestCase):
    def test_component_over_18_labels_rejected(self):
        labels = [f"x{i}" for i in range(19)]
        engine = make_engine({"L1": labels})
        # 一张票把 19 个标签连成同一个分量
        engine.submit_ticket("big", "L1", "R1", labels, 19)
        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.LIMIT_EXCEEDED)
        self.assertIn("超限", result.components[0].obstacles[0])

    def test_component_over_100_tickets_rejected(self):
        engine = make_engine({"L1": ["a", "b"]})
        for i in range(101):
            engine.submit_ticket(f"T{i:03d}", "L1", "R1", ["a", "b"], i)
        result = engine.evaluate("L1", "R1")
        self.assertEqual(result.status, Status.LIMIT_EXCEEDED)
        self.assertEqual(result.components[0].active_ticket_count, 101)

    def test_component_at_limits_is_analyzed(self):
        # 恰好 18 标签 / 100 票不应触发限流
        labels = [f"x{i}" for i in range(18)]
        engine = make_engine({"L1": labels})
        for i in range(100):
            engine.submit_ticket(f"T{i:03d}", "L1", "R1", labels, 1)
        result = engine.evaluate("L1", "R1")
        self.assertNotEqual(result.status, Status.LIMIT_EXCEEDED)
        # 100 套同标签覆盖但件数全部相同：总量一致，仍可入仓
        self.assertEqual(result.status, Status.ACCEPTED)
        self.assertEqual(result.exact_total, 1)
        self.assertEqual(result.components[0].cover_count, 100)

        # 同样 100 套覆盖但件数不同：总量不一致，判歧义
        engine2 = make_engine({"L1": labels})
        for i in range(100):
            engine2.submit_ticket(f"T{i:03d}", "L1", "R1", labels, i)
        result2 = engine2.evaluate("L1", "R1")
        self.assertEqual(result2.status, Status.AMBIGUOUS)
        self.assertIsNotNone(result2.components[0].second_evidence)


class RecomputeAndMultiLocationTests(unittest.TestCase):
    def test_withdrawal_switches_state_and_evidence(self):
        engine = make_engine({"L1": ["a", "b", "c"]})
        engine.submit_ticket("ab", "L1", "R1", ["a", "b"], 20)
        engine.submit_ticket("c", "L1", "R1", ["c"], 10)
        engine.submit_ticket("abc", "L1", "R1", ["a", "b", "c"], 30)

        before = engine.evaluate("L1", "R1")
        self.assertEqual(
            before.components[0].evidence.ticket_ids, ("ab", "c")
        )
        engine.withdraw("c")
        after = engine.evaluate("L1", "R1")
        self.assertEqual(after.status, Status.ACCEPTED)
        self.assertEqual(
            after.components[0].evidence.ticket_ids, ("abc",)
        )

    def test_multiple_locations_independent(self):
        engine = make_engine({
            "L1": ["a", "b"],
            "L2": ["x", "y"],
        })
        engine.submit_ticket("T1", "L1", "R1", ["a", "b"], 2)
        engine.submit_ticket("T2", "L2", "R1", ["x"], 1)  # L2 缺 y
        results = {r.location: r for r in engine.evaluate_all("R1")}
        self.assertEqual(results["L1"].status, Status.ACCEPTED)
        self.assertEqual(results["L1"].exact_total, 2)
        self.assertEqual(results["L2"].status, Status.NO_COVER)

    def test_no_ledger_only_facts_and_evidence(self):
        """结果对象只包含盘点事实与证据，不产生库存流水。"""
        engine = make_engine({"L1": ["a"]})
        engine.submit_ticket("T1", "L1", "R1", ["a"], 3)
        payload = engine.evaluate("L1", "R1").as_dict()
        self.assertEqual(set(payload), {
            "location", "round_id", "status", "exact_total",
            "components", "evidence",
        })
        self.assertNotIn("ledger", payload)
        self.assertNotIn("transactions", payload)


class CliTests(unittest.TestCase):
    def _run_payload(self, payload):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(payload, fh)
            path = fh.name
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main([path])
        return code, json.loads(buf.getvalue())

    def test_cli_accepted_and_rejected_and_validation_error(self):
        payload = {
            "round_id": "R1",
            "locations": {"L1": ["a", "b", "c"]},
            "tickets": [
                {"ticket_id": "abc", "location": "L1", "round_id": "R1",
                 "labels": ["a", "b", "c"], "quantity": 30},
                {"ticket_id": "ab", "location": "L1", "round_id": "R1",
                 "labels": ["a", "b"], "quantity": 20},
                {"ticket_id": "c", "location": "L1", "round_id": "R1",
                 "labels": ["c"], "quantity": 10},
            ],
        }
        code, out = self._run_payload(payload)
        self.assertEqual(code, 0)
        self.assertTrue(out["warehousable"])
        self.assertEqual(out["results"][0]["exact_total"], 30)

        payload["withdrawals"] = [{"ticket_id": "abc"}, {"ticket_id": "ab"},
                                  {"ticket_id": "c"}]
        code, out = self._run_payload(payload)
        self.assertEqual(code, 1)
        self.assertFalse(out["warehousable"])
        self.assertEqual(out["results"][0]["status"], "no_cover")

        bad = dict(payload)
        bad["tickets"] = [
            {"ticket_id": "X", "location": "L1", "round_id": "R1",
             "labels": ["nope"], "quantity": 1}
        ]
        bad["withdrawals"] = []
        code, out = self._run_payload(bad)
        self.assertEqual(code, 2)
        self.assertEqual(out["error_type"], "UnknownLabelError")

    def test_cli_run_helper_roundtrip(self):
        payload = {
            "round_id": "R1",
            "locations": {"L1": ["a"]},
            "tickets": [
                {"ticket_id": "T", "location": "L1", "round_id": "R1",
                 "labels": ["a"], "quantity": 4},
            ],
        }
        out = cli_run(payload)
        self.assertTrue(out["warehousable"])
        self.assertEqual(
            out["results"][0]["components"][0]["evidence"]["ticket_ids"], ["T"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
