"""随机小分量的暴力交叉验证。

对每个随机实例做两件独立实现并逐项对照：
1. 全枚举票据子集（小分量下可行），找出所有 exact cover 及其总量；
2. 入仓器的记忆化 DP 结论。

同时核对并查集分量切分与“标签共现图连通分量”一致。
固定随机种子保证可复现。
"""

import os
import random
import sys
import unittest
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from offline_counting import InventoryEngine, Status  # noqa: E402


def brute_force(labels, tickets):
    """返回所有 exact cover：{(票 ID 冻结集, 总量)}。"""
    index = {label: i for i, label in enumerate(labels)}
    full = (1 << len(labels)) - 1
    masked = []
    for tid, t_labels, qty in tickets:
        mask = 0
        for label in t_labels:
            mask |= 1 << index[label]
        masked.append((tid, mask, qty))

    covers = set()
    for size in range(1, len(masked) + 1):
        for subset in combinations(masked, size):
            union = 0
            total = 0
            ids = []
            ok = True
            for tid, mask, qty in subset:
                if union & mask:
                    ok = False
                    break
                union |= mask
                total += qty
                ids.append(tid)
            if ok and union == full:
                covers.add((frozenset(ids), total))
    return covers


def graph_components(labels, tickets):
    parent = {label: label for label in labels}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for _, t_labels, _ in tickets:
        t_labels = list(t_labels)
        for other in t_labels[1:]:
            union(t_labels[0], other)

    groups = {}
    for label in labels:
        groups.setdefault(find(label), set()).add(label)
    return {frozenset(g) for g in groups.values()}


class BruteForceCrossCheck(unittest.TestCase):
    def test_random_instances(self):
        rng = random.Random(20261123)
        for trial in range(600):
            n = rng.randint(1, 6)
            labels = [f"l{i}" for i in range(n)]
            m = rng.randint(0, 12)
            tickets = []
            used_ids = set()
            for k in range(m):
                tid = f"T{trial}-{k}"
                size = rng.randint(1, n)
                t_labels = rng.sample(labels, size)
                qty = rng.randint(0, 8)
                tickets.append((tid, t_labels, qty))
                used_ids.add(tid)

            engine = InventoryEngine({"L": labels})
            for tid, t_labels, qty in tickets:
                engine.submit_ticket(tid, "L", "R1", t_labels, qty)

            result = engine.evaluate("L", "R1")
            covers = brute_force(labels, tickets)

            # 1) 分量切分与共现图连通分量一致
            expected_components = graph_components(labels, tickets)
            actual_components = {frozenset(c.labels) for c in result.components}
            self.assertEqual(expected_components, actual_components)

            # 多分量实例在分量层对照；单分量在库位层对照结论
            if len(expected_components) == 1:
                comp = result.components[0]
                self._assert_matches_brute_force(comp, covers, labels)
                # 库位汇总应与唯一分量同结论
                self.assertEqual(result.status, comp.status)
                if result.status == Status.ACCEPTED:
                    self.assertEqual(
                        result.exact_total,
                        next(iter(covers))[1],
                    )
            else:
                # 多分量：逐分量用只落在该分量的票做暴力枚举对照
                for comp in result.components:
                    member = frozenset(comp.labels)
                    sub_tickets = [
                        (tid, t_labels, qty)
                        for tid, t_labels, qty in tickets
                        if frozenset(t_labels) <= member
                    ]
                    sub_covers = brute_force(list(comp.labels), sub_tickets)
                    self._assert_matches_brute_force(
                        comp, sub_covers, list(comp.labels)
                    )
                # 库位层：任一分量非接受则库位非接受；全部接受则总量为分量之和
                if all(c.status == Status.ACCEPTED for c in result.components):
                    self.assertEqual(result.status, Status.ACCEPTED)
                    self.assertEqual(
                        result.exact_total,
                        sum(c.exact_total for c in result.components),
                    )
                else:
                    self.assertNotEqual(result.status, Status.ACCEPTED)
                    self.assertIsNone(result.exact_total)

    def _assert_matches_brute_force(self, comp, covers, labels):
        distinct_totals = {total for _, total in covers}
        if not covers:
            self.assertEqual(comp.status, Status.NO_COVER)
            self.assertIsNone(comp.evidence)
            self.assertTrue(comp.obstacles)
        elif len(distinct_totals) == 1:
            self.assertEqual(comp.status, Status.ACCEPTED)
            self.assertEqual(comp.cover_count, len(covers))
            self.assertEqual(comp.exact_total, next(iter(distinct_totals)))
            # 证据票组必须真实构成一套 exact cover，且总量吻合
            ev_ids = frozenset(comp.evidence.ticket_ids)
            match = next(ids for ids, _ in covers if ids == ev_ids)
            self.assertEqual(match, ev_ids)
            self.assertEqual(
                comp.evidence.total,
                sum(q for ids, q in covers if ids == ev_ids),
            )
        else:
            self.assertEqual(comp.status, Status.AMBIGUOUS)
            self.assertIsNone(comp.exact_total)
            e1 = frozenset(comp.evidence.ticket_ids)
            e2 = frozenset(comp.second_evidence.ticket_ids)
            cover_ids = {ids for ids, _ in covers}
            self.assertIn(e1, cover_ids)
            self.assertIn(e2, cover_ids)
            self.assertNotEqual(
                comp.evidence.total, comp.second_evidence.total
            )
            self.assertIn(comp.evidence.total, distinct_totals)
            self.assertIn(comp.second_evidence.total, distinct_totals)


if __name__ == "__main__":
    unittest.main(verbosity=2)
