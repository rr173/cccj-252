#!/usr/bin/env bash
# 小样例：按导入批次还原历史日结。
# 场景：
#   批1  T-2001 rev1：A→B 10 件，01-03 发货 / 01-04 收货；
#   批2  T-2002 rev1：B→A 4 件，01-07 发货 / 01-08 收货；
#   批3  迟到更正 T-2001 rev2：发货日记错，实际是 01-02（更早业务日）。
# 批3 让当前日结重述（A 的 01-02 由 100 变为 90），但批1、批2 的历史视图
# 仍还原各批刚提交完成时实际展示的日结，仓管可据此解释当时基于账面 100 件
# 做出的出库 / 补货决定。每次 CLI 调用都重新打开工作区文件，因此本脚本同时
# 演示了历史视图与当前日结在关闭重开后仍可读取。
set -euo pipefail
cd "$(dirname "$0")"
CLI="python3 -I ../../run_close_cli.py"
DB="sample.db"
rm -f "$DB" "$DB-wal" "$DB-shm"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 建账（A、B 两个已知库位，B 在 01-06 有第二轮盘点）"
$CLI init "$DB" --config init.json

step "2. 批1：T-2001 A→B 10 件，01-03 发货 / 01-04 收货"
$CLI import "$DB" --batch batch1_book.json

step "3. 批2：T-2002 B→A 4 件，01-07 发货 / 01-08 收货"
$CLI import "$DB" --batch batch2_book.json

step "4. 批2 完成时的当前日结：A 的 01-02 账面 100 件"
$CLI close "$DB" --location A --from 2026-01-01 --to 2026-01-03

step "5. 批3（迟到更正）：T-2001 rev2，发货日更正为 01-02（更早业务日）"
$CLI import "$DB" --batch batch3_late_correction.json

step "6. 当前日结已重述：A 的 01-02 变为 90 件"
$CLI close "$DB" --location A --from 2026-01-01 --to 2026-01-03

step "7. 还原批2 刚完成时的日结：当时 A 01-02 = 100，与当前对照差 -10"
$CLI close-at "$DB" --batch 2 --location A --from 2026-01-01 --to 2026-01-03

step "8. 还原批1 刚完成时的日结（当时还没有批2 的 B→A 调拨）"
$CLI close-at "$DB" --batch 1 --location A

step "9. 批3 是最新批：历史视图与当前日结一致（差值全为 0）"
$CLI close-at "$DB" --batch 3 --location B

step "10. 只接受已成功提交的批次 / 有效日期范围 / 已知库位"
if $CLI close-at "$DB" --batch 99; then
  echo "!! 应当被拒绝" >&2; exit 1
else
  echo "(未知批次被拒绝，符合预期)"
fi
if $CLI close-at "$DB" --batch 1 --from 2026-01-06 --to 2026-01-04; then
  echo "!! 应当被拒绝" >&2; exit 1
else
  echo "(无效日期范围被拒绝，符合预期)"
fi
if $CLI close-at "$DB" --batch 1 --location Z; then
  echo "!! 应当被拒绝" >&2; exit 1
else
  echo "(未知库位被拒绝，符合预期)"
fi

step "11. 批次审计列表（每批改了哪些日结行可用 batch --id 复查）"
$CLI batches "$DB"
