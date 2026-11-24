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
让仓管知道每一批究竟改了哪些日结行，并能**还原任一批次刚提交完成时
实际展示的日结**，以解释当时的出库或补货决定。

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

### 按批次还原历史日结

- `close-at --batch N` 还原**批次 N 提交完成时**工作区实际展示的日结，并与
  当前日结对照：每行含当时件数（`quantity_then`）、当前件数（`quantity_now`）
  与差值（`delta = 当前 − 当时`），按库位、业务日排序；可用 `--location` /
  `--from` / `--to` 限定库位与业务日期范围。
- 历史视图由审计表推出：以当前日结为基准，把晚于该批的 `batch_changes` 按批号
  **倒序**逐一撤销（每行回到该批修改前的件数）。批次写入与审计在同一事务，
  还原结果永远对应一个完整提交的批次，不会读到批执行到一半的状态。
- 后续批次只追加新审计行：即使补录**更早业务日**的迟到更正，较早批次的历史
  视图也不被改写；当前日结仍显示最新有效修订重述后的结果。
- 只接受已成功提交的批次：未知批次（`WorkspaceStateError`）、无效日期范围
  （`InvalidRecordError`）、不存在的库位（`WorkspaceStateError`）明确报错，
  不返回看似成功的空结果。历史视图与当前日结一样，关闭并重新打开工作区后
  仍可读取。

## 用法

```bash
# 建账：品项 + 末个关账日 + 一组固定盘点锚点（新建 SQLite 文件）
python3 -I run_close_cli.py init ws.db --config init.json

# 批量导入调拨修订（--batch - 从标准输入读）
python3 -I run_close_cli.py import ws.db --batch batch.json

# 读取当前日结（可按库位 / 日期段过滤）
python3 -I run_close_cli.py close ws.db [--location B] [--from 2026-01-04] [--to 2026-01-06]

# 按批次还原历史日结，并与当前日结对照（当时件数 / 当前件数 / 差值）
python3 -I run_close_cli.py close-at ws.db --batch 2 [--location B] [--from 2026-01-04] [--to 2026-01-06]

# 盘点锚点 / 调拨版本（含已失效版本与生效标记）
python3 -I run_close_cli.py anchors ws.db
python3 -I run_close_cli.py versions ws.db [--doc T-1001]

# 批次审计：每批改了哪些日结行
python3 -I run_close_cli.py batches ws.db
python3 -I run_close_cli.py batch ws.db --id 2

# 只读调拨追溯：一次盘点开启的存量后来随哪些调拨发走、现在到哪里复核
python3 -I run_close_cli.py trace ws.db --location A --date 2026-01-01 \
  --cutoff 2026-01-10 --manifest manifest.json
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

### 作业顺位清单 JSON（trace 用）

```json
{
  "entries": [
    {"doc_no": "T-1001", "revision": 1, "side": "ship", "rank": 1},
    {"doc_no": "T-1001", "revision": 1, "side": "recv", "rank": 2},
    {"doc_no": "T-1002", "revision": 1, "ship": 1, "recv": 2}
  ]
}
```

- 每行给一条腿（`side` + `rank`），或用一行 `ship` / `recv` 同时给同一单号修订
  的两侧顺位；两种写法等价，也可直接把数组作为清单。
- 清单**只提供顺位**：发/收库位与日期一律取工作区生效版本，清单里的任何其它
  字段都会被拒绝。
- 必须恰好覆盖业务日不晚于截至日的全部现行生效调拨腿；同日顺位全仓唯一，
  当天发收的调拨发货顺位须小于收货顺位。缺腿、重复、引用已失效修订/撤销版本、
  包含范围外腿都明确报错（`InvalidRecordError`，退出码 2），不交付部分追溯结果。

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

## 只读调拨追溯（trace）

仓管知道每个库位最后有多少货，却不知道某次盘点确认的那批存量后来分别随哪些
调拨发走、现在应该去哪里复核。`trace` 在**不改写任何盘点 / 调拨 / 日结数据**的
前提下，把一次起点盘点开启的存量沿现行生效调拨追踪到截至日。

### 归属规则

- **起点**：指定库位 + 业务日的 `accepted` 盘点；起点件数 = 该盘点 `exact_total`。
  起点必须是截至日已发生的盘点，截至日不得晚于工作区末个关账日。
- **取用顺序**：每次发货先用该库位**最早进入、尚未用完**的存量，跨批取货保留
  取用先后；收货时整批以收货时刻进入目的库位，批内仍保留原发货的取用顺序。
- **来源不明补足**：库位缺多少就为这次发货补多少来源不明的货，排在实取到的货
  之后；它也可随货到达下游，但绝不用将来的收货倒填。
- **首次盘点前**不假定库位有开账存量，但已发生的收货照常进入。
- **后续盘点**均发生在当天调拨结束后：盘前未发出的货中止来源追溯（计入
  “盘点中止”），盘后按盘点数开始一份不可追溯的新存量；已发或在途的货不受影响。
- **不重复计入**：经多次调拨的起点货只记在签发它的那次发货上；回到起点库位
  也不再算作新增起点。报告按业务记录合并来源数量，零贡献不列。

### 报告内容

```json
{
  "status": "ok", "read_only": true,
  "start": {"location": "A", "biz_date": "2026-01-01", "round": "R1", "quantity": 2},
  "cutoff": "2026-01-10",
  "shipments": [
    {"doc_no": "T-1001", "revision": 1,
     "from_location": "A", "from_date": "2026-01-02",
     "to_location": "B", "to_date": "2026-01-02",
     "quantity": 6, "origin_quantity": 2,
     "sources": [{"source": "receipt", "doc_no": "T-0900", "revision": 1,
                  "location": "A", "biz_date": "2026-01-01", "quantity": 2}]}
  ],
  "remaining": [{"location": "C", "quantity": 2}],
  "in_flight": [{"doc_no": "T-1006", "revision": 1,
                 "from_location": "C", "from_date": "2026-01-09",
                 "to_location": "B", "to_date": "2026-01-11",
                 "quantity": 1}],
  "stopped_at_anchor": [{"location": "B", "biz_date": "2026-01-06",
                         "round": "R2", "quantity": 1}],
  "totals": {"start_quantity": 2, "remaining": 0, "in_flight": 1,
             "stopped_at_anchor": 1, "destination_total": 2, "balanced": true}
}
```

- `shipments`：含起点货的每一笔发货（单号、修订号、发/收库位与日期、
  `origin_quantity` 是本次带走的起点件数），`sources` 是其中各有多少来自发货
  库位的哪次入货（起点盘点 `anchor` 或某笔收货 `receipt`）。
  日期排序、单号排序，**不随清单行序改变**。
- `remaining` / `in_flight` / `stopped_at_anchor`：截至日的三类去向——仍在各
  库位的起点件数、已发未收的各单起点件数、因后续盘点中止追溯的各盘点件数；三者和
  `destination_total` 必须等于 `start_quantity`（`balanced`）。
- 数量全程按整数批次片段推进，不按单件或逐条完整路径展开，拆开再汇合的多路
  调拨不会撑大报告或工作状态。

### 清单校验与错误

清单缺腿、重复、引用已失效修订或撤销版本、包含不在范围内的腿，或起点不是
截至日已发生的 `accepted` 盘点、截至日超过末个关账日，都输出
`{"status": "error", ...}`、退出码 2，且不交付部分追溯结果。

## 小样例：按批次还原历史日结

`examples/batch_history/run_sample.sh` 完整演示（可直接运行）：

1. **建账**：A、B 两个已知库位；B 在 `2026-01-06` 有第二轮 `accepted` 盘点。
2. **批 1** `T-2001 rev1`：A→B 10 件，发货 `01-03`、收货 `01-04`。
3. **批 2** `T-2002 rev1`：B→A 4 件，发货 `01-07`、收货 `01-08`。
   此时 A 的 `01-02` 账面 100 件。
4. **批 3（迟到更正）** `T-2001 rev2`：发货日记错，实际是 `01-02`（更早业务日）。
   当前日结重述：A 的 `01-02` 由 100 变为 90。
5. **还原批 2 刚完成时的日结**：`close-at --batch 2` 显示 A `01-02` 当时 100、
   当前 90、差值 −10——历史视图不被批 3 改写，可解释当时基于账面 100 件的决定。
6. **还原批 1**：当时还没有批 2 的调拨（A `01-08` 起当时 90、当前 94）。
7. **最新批**的历史视图与当前日结一致（差值全为 0）。
8. **错误**：未知批次、无效日期范围、不存在的库位均被拒绝，退出码 2。

```bash
cd examples/batch_history && ./run_sample.sh
```

## 小样例：只读调拨追溯

`examples/trace/run_sample.sh` 完整演示（可直接运行）：

1. **建账**：A/B/C 三个库位；A 起点盘点 `2026-01-01 R1 = 2` 件，
   B 在 `2026-01-06` 有第二轮盘点。
2. **A→B** `T-1001`：起点 2 件与 B 原有 4 件同批收货。
3. **B→C** `T-1002`：B 先发 4 件，取的是 B 原有货 → 该发货不带起点货，
   报告不列。
4. **B→A** `T-1003`：1 件起点货绕回起点库位，不算新增；**A→C** `T-1004`
   再发 2 件（1 件起点 + 1 件来源不明）。
5. **B 盘点** `01-06 R2`：盘前仍在 B 的 1 件起点货中止追溯。
6. **C→B** `T-1006`：发货 `01-09`、收货 `01-11`，截至 `01-10` 时 1 件起点货
   仍在途。
7. 截至 `2026-01-10`：在库 0 + 在途 1 + 盘点中止 1 = 起点 2 件。
8. **只读性**：追溯前后 `close` 输出逐字节一致。
9. **清单校验**：漏掉 `T-1006` 的发货腿被拒绝，退出码 2。

```bash
cd examples/trace && ./run_sample.sh
```

## 测试

```bash
python3 -I tests/test_inventory_close.py
python3 -I tests/test_inventory_trace.py
```

41 个用例覆盖：建账铺行与锚点约束、双端各自日期记账、跨盘点日更正、
幂等重送、同/异内容冲突（含已失效版本）、整批回滚不留痕、旧版本补录、
批内顺序无关、撤销、首锚点前与锚点当天的腿不影响日结、负账面量、
仅改数量、净值未变不报、多单据同批原子生效、另一端无余额行、
记录校验、关闭重开后的持久性、按批次还原历史日结（当时/当前/差值、
迟到更正不改写较早批次视图、空改动批、过滤与排序、未知批次/无效日期
范围/未知库位报错），以及 CLI 全链路。

`tests/test_inventory_trace.py` 另有 40 个用例：起点件数与逐笔发货来源、
多跳只归最近一次入货、FIFO 与收货批内顺序（先 4 件别的后 2 件起点 → 先发
4 件不带起点）、库位不足的来源不明补足、后续盘点中止与盘后新存量、起点
在途、起点当天发货、大件数与多路拆并、清单缺腿/重复/失效修订/撤销/范围外
/非正顺位/同日先发后收校验、报告顺序与清单行序无关，以及追溯只读且幂等的
文件级校验。

```bash
python3 -I tests/test_inventory_trace.py
```

## 目录结构（新增）

```
inventory_close/
  errors.py         # 可区分的业务异常（配置/记录/冲突/状态）
  store.py          # 建账、批量导入、增量重算、读取与审计（单事务）
  trace.py          # 只读调拨追溯（一致读取 + 批量片段 FIFO）
  cli.py            # JSON 进 / JSON 出
tests/
  test_inventory_close.py
  test_inventory_trace.py
examples/
  daily_close/      # 跨盘点日更正小样例（init + 4 个批次 + run_sample.sh）
  batch_history/    # 按批次还原历史日结小样例（init + 3 个批次 + run_sample.sh）
  trace/            # 只读调拨追溯小样例（init + 调拨 + 清单 + run_sample.sh）
run_close_cli.py    # 顶层包装，保证 python -I 下也能导入本地包
```
