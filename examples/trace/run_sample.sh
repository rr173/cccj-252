#!/usr/bin/env bash
# 小样例：只读调拨追溯。
# 场景：
#   A 起点盘点 2 件（01-01 R1）；
#   01-02 T-1001 把 A 的 2 件起点货运到 B，与 B 原有的 4 件同批；
#   01-03 T-1002 从 B 发走 4 件（先取 B 原有货，不带起点货）；
#   01-04 T-1003 从 B 发 1 件起点货回 A（起点货绕回起点库位，不算新增）；
#   01-05/06 T-1004 从 A 发 2 件（1 件起点 + 1 件来源不明）到 C；
#   01-06 B 有第二轮盘点，盘前仍在 B 的 1 件起点货在此中止追溯；
#   01-07/08 T-1005 从 C 发 3 件（全是更早的别的存量）；
#   01-09 T-1006 从 C 发 2 件（1 件为起点货），01-11 才收货 → 截至 01-10 在途。
#
# 截至 2026-01-10 的三类去向：C 在库 0、T-1006 在途 1、B 盘点中止 1，合计 2。
set -euo pipefail
cd "$(dirname "$0")"
CLI="python3 -I ../../run_close_cli.py"
DB="sample.db"
rm -f "$DB" "$DB-wal" "$DB-shm"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 建账（A/B/C 三个库位，B 在 01-06 有第二轮盘点）"
$CLI init "$DB" --config init.json

step "2. 导入 6 笔调拨（T-1006 收货日在截至日之后）"
$CLI import "$DB" --batch transfers.json

step "3. 起点：A 库位 2026-01-01 的盘点（R1，2 件），截至 2026-01-10"
$CLI trace "$DB" --location A --date 2026-01-01 --cutoff 2026-01-10 \
  --manifest manifest.json

step "4. 只读性：追溯前后日结完全一致"
$CLI close "$DB" --location A > /tmp/trace_before_A.json
$CLI close "$DB" --location A > /tmp/trace_after_A.json
diff /tmp/trace_before_A.json /tmp/trace_after_A.json && echo "(日结未变)"

step "5. 清单必须恰好覆盖范围内的全部生效腿：漏掉 T-1006 的发货腿会被拒绝"
python3 - "$DB" <<'PY'
import json, subprocess, sys
manifest = json.load(open("manifest.json"))
manifest["entries"] = [e for e in manifest["entries"] if e["doc_no"] != "T-1006"]
proc = subprocess.run(
    [sys.executable, "-I", "../../run_close_cli.py", "trace", sys.argv[1],
     "--location", "A", "--date", "2026-01-01", "--cutoff", "2026-01-10",
     "--manifest", "-"],
    input=json.dumps(manifest), text=True, capture_output=True,
)
assert proc.returncode == 2, proc.stdout
print(proc.stdout.strip())
print("(缺腿被拒绝，符合预期)")
PY
