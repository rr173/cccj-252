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

---

# 离线库存日结工作区（inventory_close）

下游配套工具：把盘点入仓器核定的盘点事实当作**固定锚点**，在其上维护
**一个品项**在多个库位间的逐日关账库存。出入库仅来自库位间调拨；调拨单
常在关账后才更正（例如收货日记错），本工作区让报表读到修正后的日结值，
也让仓管知道每一批究竟改了哪些日结行。

## 数据规则

### 盘点锚点（建账时固定）

- 锚点 = 业务日 + 盘点输出中的库位、轮次、`status`、`exact_total`；
  **只接受 `accepted` 的事实**，其余状态原样存档但不参与日结。
- 锚点不晚于关账末日；同一库位同日最多一条。
- 有 `accepted` 锚点的库位即**已知库位**，调拨只允许在已知库位间发生。

### 日结行

- 每个已知库位从自己的首个锚点到关账末日逐日一行：库位、业务日、日末件数、
  最近一次盘点的日期及轮次。
- 锚点当日直接用盘点数（锚点是当日关账后的实数），**不叠加当日调拨**；
  其他日期 = 前一日余额 + 当天收货 − 当天发货。
- 首个锚点之前不推算余额；日末账面量**可以为负**。

### 调拨版本

- 调拨按批次提交，每版含单号 + 正整数修订号；**单号只按收到过的最高修订号生效**，
  版本整单替换、不继承前版字段，更正可改库位、日期或数量。
- 有效版本：不同的已知发货/收货库位、各自业务日、同一个正整数件数，
  发货日不晚于收货日，两日均不晚于关账末日；撤销版本（`void`）没有发收货贡献。
- 两个库位各自在自己的日期记账，即使另一端当天还没有余额，也不影响本端规则。
- 相同单号 + 修订号**同内容可重送**（幂等）；**异内容是冲突，哪怕该版本已不生效**。
- 合法旧版本可以补录（入库存档），但不能覆盖新版本；批内顺序不影响结果。
- 任一记录无效或冲突 → **整批不留痕**。

### 一致性与增量重算

- 一次批量导入 = 一个 SQLite 事务（WAL + `synchronous=FULL`）。写入途中中断后
  重开，只能读到完整的批前或批后状态：不会留下半张调拨，也不会让日结和
  生效版本分属两批。重送已成功导入的内容不会重复改数。
- 每天关账历史全部保留（`daily_close` 逐日一行），迟到更正**不重跑整个库位**：
  只重算原生效或新生效调拨腿能影响的区间——从该腿业务日到下一盘点锚点之前
  （无下一锚点则到关账末日）；首锚点之前和锚点当天的腿不影响日结，不扫描对应区间。
- 重算以受影响区间**前一日的日结值**为基准，逐日累加当前生效腿的净额；
  **净值未变的行不报也不重写**。一次成功导入返回本批真正改变的日结行
  （含修改前后件数，按库位再按日期排序）、涉及的单号与锚点、扫描区间和基准值；
  同样的内容也落入 `batches` / `batch_documents` / `batch_changes` 审计表，
  供事后复查。

## 用法

```bash
# 建账：品项 + 末个关账日 + 一组固定盘点锚点（新建 SQLite 文件）
python3 -I run_close_cli.py init ws.db --config init.json

# 批量导入调拨修订（--batch - 从标准输入读）
python3 -I run_close_cli.py import ws.db --batch batch.json

# 读取当前日结（可按库位 / 日期段过滤）
python3 -I run_close_cli.py close ws.db [--location B] [--from 2026-01-04] [--to 2026-01-06]

# 盘点锚点 / 调拨版本（含已失效版本与生效标记）
python3 -I run_close_cli.py anchors ws.db
python3 -I run_close_cli.py versions ws.db [--doc T-1001]

# 批次审计：每批改了哪些日结行
python3 -I run_close_cli.py batches ws.db
python3 -I run_close_cli.py batch ws.db --id 2
```

业务错误输出 `{"status": "error", "error_type": ..., "reason": ...}`，退出码 2，
且整批不留痕。

### 建账配置 JSON

```json
{
  "item": "SKU-0001",
  "closing_end": "2026-01-10",
  "anchors": [
    {"location": "A", "biz_date": "2026-01-01", "round": "R1",
     "status": "accepted", "exact_total": 100},
    {"location": "B", "biz_date": "2026-01-01", "round": "R1",
     "status": "accepted", "exact_total": 0},
    {"location": "B", "biz_date": "2026-01-06", "round": "R2",
     "status": "accepted", "exact_total": 7}
  ]
}
```

### 批次 JSON

```json
{
  "records": [
    {"doc_no": "T-1001", "revision": 1, "type": "transfer",
     "from_location": "A", "from_date": "2026-01-03",
     "to_location": "B", "to_date": "2026-01-04", "quantity": 10},
    {"doc_no": "T-1002", "revision": 3, "type": "void"}
  ]
}
```

### 导入结果（节选）

```json
{
  "status": "ok",
  "batch_id": 2,
  "versions_inserted": 1,
  "documents": [{"doc_no": "T-1001", "old_effective_revision": 1,
                 "new_effective_revision": 2, "effective_changed": true}],
  "scanned_ranges": [{"location": "B", "from": "2026-01-04", "to": "2026-01-05"}],
  "anchors": [{"location": "B", "biz_date": "2026-01-01", "round": "R1"}],
  "baselines": [{"location": "B", "biz_date": "2026-01-03", "quantity": 0}],
  "changed_rows": [{"location": "B", "biz_date": "2026-01-04",
                    "old_quantity": 10, "new_quantity": 0,
                    "anchor_date": "2026-01-01", "anchor_round": "R1"}]
}
```

## 小样例：一次跨盘点日的调拨更正

`examples/daily_close/run_sample.sh` 完整演示（可直接运行）：

1. **建账**：A、B 两个已知库位；B 在 `2026-01-06` 有第二轮 `accepted` 盘点
   （`exact_total = 7`）。
2. **首批** `T-1001 rev1`：A→B 10 件，发货 `01-03`、收货 `01-04`。
   B 的 `01-04`、`01-05` 变为 10；`01-06` 锚点日仍是盘点数 7（不叠加当日之前在途）。
3. **更正** `rev2`：收货日记错，由 `01-04` 改为 `01-07`（跨过 `01-06` 盘点日）。
   发货腿未变、净额为零 → A 不扫描；B 只重算 `[01-04, 01-05]` 与 `[01-07, 01-10]`
   两个区间，以 `01-03`（0）和 `01-06`（7）的日结值为基准：
   `01-04`、`01-05` 回到 0，`01-07` 至 `01-10` 变为 17，锚点日 `01-06` 不动。
4. **幂等重送**同一批：`versions_inserted = 0`、`changed_rows = []`。
5. **冲突**：`rev1` 异内容重送（哪怕已不生效）→ 整批拒绝，退出码 2。
6. **撤销** `rev3 void`：调拨腿整体撤下，日结回到锚点实数。
7. **审计**：`batches` / `batch --id 2` 复查每一批改了哪些日结行。

```bash
cd examples/daily_close && ./run_sample.sh
```

## 测试

```bash
python3 -I tests/test_inventory_close.py
```

31 个用例覆盖：建账铺行与锚点约束、双端各自日期记账、跨盘点日更正、
幂等重送、同/异内容冲突（含已失效版本）、整批回滚不留痕、旧版本补录、
批内顺序无关、撤销、首锚点前与锚点当天的腿不影响日结、负账面量、
仅改数量、净值未变不报、多单据同批原子生效、另一端无余额行、
记录校验、关闭重开后的持久性，以及 CLI 全链路。

## 目录结构（新增）

```
inventory_close/
  errors.py         # 可区分的业务异常（配置/记录/冲突/状态）
  store.py          # 建账、批量导入、增量重算、读取与审计（单事务）
  cli.py            # JSON 进 / JSON 出
tests/
  test_inventory_close.py
examples/
  daily_close/      # 跨盘点日更正小样例（init + 4 个批次 + run_sample.sh）
run_close_cli.py    # 顶层包装，保证 python -I 下也能导入本地包
```
