"""离线库存日结工作区的命令行入口。

用法::

    python3 -I run_close_cli.py init    WORKSPACE.db --config init.json
    python3 -I run_close_cli.py import  WORKSPACE.db --batch batch.json
    python3 -I run_close_cli.py close   WORKSPACE.db [--location A] [--from D] [--to D]
    python3 -I run_close_cli.py close-at WORKSPACE.db --batch 2 [--location A] [--from D] [--to D]
    python3 -I run_close_cli.py anchors WORKSPACE.db
    python3 -I run_close_cli.py versions WORKSPACE.db [--doc T-1001]
    python3 -I run_close_cli.py batches WORKSPACE.db
    python3 -I run_close_cli.py batch   WORKSPACE.db --id 2
    python3 -I run_close_cli.py trace   WORKSPACE.db --location A \\
        --date 2026-01-01 --cutoff 2026-01-10 --manifest manifest.json

``--config`` / ``--batch`` 为 ``-`` 时从标准输入读取 JSON。
所有输出均为 JSON；业务错误以 ``{"status": "error", ...}`` 返回，退出码 2，
且整批不留痕。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .errors import WorkspaceError
from . import store
from .trace import trace_transfers


def _load_json(path: str) -> dict:
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        raise WorkspaceError(f"读取文件失败：{exc}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"JSON 解析失败：{exc}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_close_cli", description="离线库存日结工作区"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="建账：品项 + 关账末日 + 固定盘点锚点")
    p.add_argument("db", help="SQLite 工作区文件路径（不得已存在）")
    p.add_argument("--config", required=True, help="建账配置 JSON 路径，- 表示标准输入")

    p = sub.add_parser("import", help="批量导入调拨修订")
    p.add_argument("db", help="SQLite 工作区文件路径")
    p.add_argument("--batch", required=True, help="批次 JSON 路径，- 表示标准输入")

    p = sub.add_parser("close", help="读取当前日结")
    p.add_argument("db")
    p.add_argument("--location", help="只看某个库位")
    p.add_argument("--from", dest="date_from", help="起始业务日 YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="截止业务日 YYYY-MM-DD")

    p = sub.add_parser("close-at", help="按批次还原历史日结，并与当前日结对照")
    p.add_argument("db")
    p.add_argument("--batch", type=int, required=True, help="已成功提交的批次号")
    p.add_argument("--location", help="只看某个库位")
    p.add_argument("--from", dest="date_from", help="起始业务日 YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="截止业务日 YYYY-MM-DD")

    p = sub.add_parser("anchors", help="列出盘点锚点")
    p.add_argument("db")

    p = sub.add_parser("versions", help="列出调拨版本（含已失效版本）")
    p.add_argument("db")
    p.add_argument("--doc", help="只看某个单号")

    p = sub.add_parser("batches", help="批次列表")
    p.add_argument("db")

    p = sub.add_parser("batch", help="批次明细：本批改了哪些日结行")
    p.add_argument("db")
    p.add_argument("--id", type=int, required=True, help="批次号")

    p = sub.add_parser(
        "trace", help="只读调拨追溯：一次盘点开启的存量现在到哪里复核"
    )
    p.add_argument("db")
    p.add_argument("--location", required=True, help="起点盘点所在库位")
    p.add_argument("--date", required=True, help="起点盘点业务日 YYYY-MM-DD")
    p.add_argument("--cutoff", required=True,
                   help="截至日 YYYY-MM-DD（含），不得晚于末个关账日")
    p.add_argument("--manifest", required=True,
                   help="作业顺位清单 JSON 路径，- 表示标准输入")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            output = store.init_workspace(args.db, _load_json(args.config))
        elif args.command == "import":
            output = store.import_batch(args.db, _load_json(args.batch))
        elif args.command == "close":
            output = store.read_close(args.db, location=args.location,
                                      date_from=args.date_from, date_to=args.date_to)
        elif args.command == "close-at":
            output = store.read_close_at_batch(args.db, args.batch,
                                               location=args.location,
                                               date_from=args.date_from,
                                               date_to=args.date_to)
        elif args.command == "anchors":
            output = store.list_anchors(args.db)
        elif args.command == "versions":
            output = store.list_versions(args.db, doc_no=args.doc)
        elif args.command == "batches":
            output = store.list_batches(args.db)
        elif args.command == "batch":
            output = store.get_batch(args.db, args.id)
        elif args.command == "trace":
            output = trace_transfers(
                args.db, location=args.location, biz_date=args.date,
                cutoff=args.cutoff, manifest=_load_json(args.manifest),
            )
        else:  # pragma: no cover - argparse 已拦截
            raise WorkspaceError(f"未知命令：{args.command}")
    except WorkspaceError as exc:
        json.dump(
            {"status": "error", "error_type": type(exc).__name__, "reason": str(exc)},
            sys.stdout, ensure_ascii=False, indent=2,
        )
        sys.stdout.write("\n")
        return 2
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
