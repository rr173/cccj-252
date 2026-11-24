#!/usr/bin/env bash
# 小样例：一次跨盘点日的调拨更正。
# 场景：B 库位 2026-01-06 有一轮 accepted 盘点（exact_total=7）。
#   1) T-1001 rev1 记 A→B 10 件，收货日记为 01-04（锚点之前）；
#   2) 关账后发现收货日记错，rev2 把收货日更正为 01-07（锚点之后）；
#   3) 演示幂等重送、冲突拒绝、撤销与批次审计。
set -euo pipefail
cd "$(dirname "$0")"
CLI="python3 -I ../../run_close_cli.py"
DB="sample.db"
rm -f "$DB" "$DB-wal" "$DB-shm"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 建账（A、B 两个已知库位，B 在 01-06 有第二轮盘点）"
$CLI init "$DB" --config init.json

step "2. 首批：T-1001 rev1，A→B 10 件，发货 01-03 / 收货 01-04"
$CLI import "$DB" --batch batch1_book.json

step "3. 当前日结（B：01-04、01-05 各 10 件，01-06 锚点日仍是盘点数 7）"
$CLI close "$DB" --location B

step "4. 跨盘点日更正：rev2 把收货日改为 01-07（只重算 B 的两个受影响区间）"
$CLI import "$DB" --batch batch2_correct_recv_date.json

step "5. 更正后的 B 库位日结"
$CLI close "$DB" --location B

step "6. 幂等重送同一批：版本不重复入库、日结不重复改数"
$CLI import "$DB" --batch batch2_correct_recv_date.json

step "7. 冲突：rev1 异内容重送（哪怕已不生效）→ 整批拒绝，退出码 2"
if $CLI import "$DB" --batch batch3_conflict.json; then
  echo "!! 应当被拒绝" >&2; exit 1
else
  echo "(批被拒绝，符合预期)"
fi

step "8. 撤销：rev3 void，调拨腿整体撤下，日结回到锚点实数"
$CLI import "$DB" --batch batch4_void.json
$CLI close "$DB" --location B

step "9. 批次审计：仓管可复查每一批改了哪些日结行"
$CLI batches "$DB"
$CLI batch "$DB" --id 2
