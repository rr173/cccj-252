# 离线盘点入仓器

把箱级 / 件级盘点票据变成“**每个预期标签恰好被数一次**”的可信盘点事实。

核心原则：**不是按来源优先级挑一个数**。入仓前必须先证明，库位的每个预期标签
都能被一组**互不重叠的现有票据**完整覆盖（exact cover）；当存在多套完整覆盖时，
只有它们的总件数全部一致，库位才允许入仓。

## 判定规则

1. **只用调用方给定的标签目录**：票据引用目录外标签直接拒绝（`UnknownLabelError`），
   系统不生成任何虚拟标签。
2. **整票数量不摊分**：每张票给的是覆盖整体的非负整数件数，只对整票有效；
   一套覆盖的总量 = 其中各票整票件数之和。
3. **轮次资格**：每张票带 `round_id`，不同盘点轮次的票据**禁止**拼成一次完整盘点，
   查询必须指定轮次。
4. **撤票**：撤票使对应票据失效，非撤回事项保留；同一轮次撤票后直接重算相关库位，
   结论与证据随状态整体切换，不保留跨库位历史。
5. **输入错误显式区分**：
   - 完全相同重送 → 幂等（返回 `False`，状态不变）；
   - 同票 ID 内容不一致 → `TicketConflictError`；
   - 未知标签 → `UnknownLabelError`；票内重复标签 → `DuplicateLabelError`；
   - 撤票引用不存在 → `UnknownWithdrawalRefError`；撤一张已撤的票 → 幂等。
6. **分量切分**：按标签重叠关系（并查集）把一个库位切成互无共同票据的分量。
   每个分量独立判定，**不把各分量的覆盖组合相乘展开**，也不允许用一个分量的
   反向差额把另一个分量的歧义“平均掉”。
7. **规模上限**：单分量至多 18 个标签、100 张有效票，超限返回
   `limit_exceeded` 并明确拒绝枚举。
8. **三种结论**：
   - `accepted`：所有完整覆盖总量相同 → 交付精确件数 + 至少一套有效票组证据；
   - `no_cover`：不存在恰好覆盖 → 报告所属分量与可复查障碍（无票标签 / 死路票据链）；
   - `ambiguous`：多套完整覆盖总量不一致 → 给两套票组 ID 及各自数量。

成品只产出盘点事实与证据，**不做库存流水入账**。

## 算法

- 标签集合用位掩码表示，exact cover 用记忆化 DFS 求解，状态按“剩余标签掩码”定义，
  状态空间至多 `2^n`（n ≤ 18），最坏操作量约为 `票数 × 2^n`，在约定上限内可控，
  不会随票数指数爆炸。
- 每个状态聚合：是否可覆盖、覆盖数（封顶统计）、总量是否唯一、一套证据票组；
  发现同状态可由不同路径得到不同总量时立即标记 `ambiguous` 并保留两套冲突证据，
  不枚举全部组合。
- 分支采用 MRV（可选票据最少的标签优先），同时服务于快速收敛与障碍诊断。

## 目录结构

```
offline_counting/
  models.py         # Ticket / CoverEvidence / ComponentResult / LocationResult
  errors.py         # 可区分的输入校验异常
  cover_analysis.py # 位掩码 exact cover + 总量一致性 DP（逐分量）
  engine.py         # 票据/撤票状态、并查集分量切分、库位汇总
  cli.py            # JSON 进 / JSON 出
tests/
  test_offline_counting.py
examples/
  01_consistent_covers.json            # 整箱与分件两套覆盖，总量一致 → 可供数
  02_withdraw_abc_no_cover.json        # 撤 abc 后 ab/bc 重叠，无不重叠覆盖
  03_ambiguous_totals.json             # 两套覆盖总量 31 vs 30 → 歧义，附两套证据
  04_two_components_opposite_gaps.json # 两分量反向差额，不得互相平均
  05_unknown_label_rejected.json       # 未知标签被拒绝，无虚拟标签
run_cli.py         # 顶层包装，保证 python -I 下也能导入本地包
```

## 用法

### 库 API

```python
from offline_counting import InventoryEngine, Status

engine = InventoryEngine({"LOC-A": ["a", "b", "c"]})
engine.submit_ticket("abc", "LOC-A", "R1", ["a", "b", "c"], 30)
engine.submit_ticket("ab",  "LOC-A", "R1", ["a", "b"], 20)
engine.submit_ticket("c",   "LOC-A", "R1", ["c"], 10)

result = engine.evaluate("LOC-A", "R1")
assert result.status is Status.ACCEPTED
assert result.exact_total == 30
# 证据（至少一套有效票组）
for ev in result.evidence:
    ...
engine.withdraw("abc")          # 同一轮次撤票后重算
result2 = engine.evaluate("LOC-A", "R1")
```

### 命令行

```bash
python3 -I run_cli.py examples/01_consistent_covers.json     # 退出码 0：可入仓
python3 -I run_cli.py examples/02_withdraw_abc_no_cover.json # 退出码 1：no_cover
python3 -I run_cli.py examples/03_ambiguous_totals.json      # 退出码 1：ambiguous
python3 -I run_cli.py examples/05_unknown_label_rejected.json# 退出码 2：输入校验错误
cat payload.json | python3 -I run_cli.py -
```

注意：`python -I` 不会把当前目录加入模块搜索路径，因此请始终通过顶层
`run_cli.py`（内部自行注入包路径）调用；非隔离环境下也可用
`python3 -m offline_counting.cli <payload>`。

输入 JSON：

```json
{
  "round_id": "R1",
  "locations": {"LOC-A": ["a", "b", "c"]},
  "tickets": [
    {"ticket_id": "abc", "location": "LOC-A", "round_id": "R1",
     "labels": ["a", "b", "c"], "quantity": 30}
  ],
  "withdrawals": [{"ticket_id": "abc"}],
  "query_locations": ["LOC-A"]
}
```

## 测试

```bash
python3 -I tests/test_offline_counting.py
```

23 个用例覆盖：整箱/分件一致可供数、撤件级票后仍有覆盖、撤整箱票后标签仍在但
无互不重叠覆盖、歧义双证据、多分量切分与求和、反向差额不可平均、轮次隔离、
幂等/冲突/未知标签/重复标签/撤票引用区分、18/100 上限与边界、撤票证据切换、
多库位独立以及 CLI 全链路。
