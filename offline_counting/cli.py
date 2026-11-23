"""离线盘点入仓器的命令行入口。

输入 JSON 结构（只支持调用方给定的标签目录，不生成虚拟标签）::

    {
      "round_id": "R1",
      "locations": {"LOC1": ["a", "b", "c"]},
      "tickets": [
        {"ticket_id": "T1", "location": "LOC1", "round_id": "R1",
         "labels": ["a", "b", "c"], "quantity": 30}
      ],
      "withdrawals": [{"ticket_id": "T1"}],
      "query_locations": ["LOC1"]          // 可选，缺省评估全部库位
    }

输出每个库位的判定、逐分量障碍/歧义证据，以及可入仓时的精确件数与票组。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .engine import InventoryEngine
from .errors import InventoryValidationError
from .models import LocationResult


def build_engine_from_payload(payload: dict) -> tuple:
    if not isinstance(payload, dict):
        raise InventoryValidationError("输入必须是 JSON 对象")
    catalog = payload.get("locations")
    if not isinstance(catalog, dict) or not catalog:
        raise InventoryValidationError("locations 必须是非空的 {库位: [标签...]} 映射")

    engine = InventoryEngine(catalog)
    round_id = payload.get("round_id")
    if round_id is not None and (not isinstance(round_id, str) or not round_id):
        raise InventoryValidationError("round_id 必须是非空字符串")

    for raw in payload.get("tickets", []):
        engine.submit_ticket(
            ticket_id=raw["ticket_id"],
            location=raw["location"],
            round_id=raw["round_id"],
            labels=raw["labels"],
            quantity=raw["quantity"],
        )

    for raw in payload.get("withdrawals", []):
        if isinstance(raw, dict):
            ref = raw["ticket_id"]
        else:  # 也允许直接给票 ID 字符串
            ref = raw
        engine.withdraw(ref)

    query = payload.get("query_locations")
    if query is not None and not isinstance(query, list):
        raise InventoryValidationError("query_locations 必须是库位 ID 列表")
    return engine, round_id, query


def run(payload: dict) -> dict:
    engine, round_id, query = build_engine_from_payload(payload)
    if round_id is None:
        raise InventoryValidationError("必须指定 round_id 才能判定盘点轮次")

    targets: Sequence[str] = query if query is not None else engine.locations
    results: List[LocationResult] = [engine.evaluate(loc, round_id) for loc in targets]
    accepted = all(r.status.value == "accepted" for r in results)
    return {
        "round_id": round_id,
        "warehousable": accepted,
        "results": [r.as_dict() for r in results],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="离线盘点入仓器")
    parser.add_argument(
        "payload",
        nargs="?",
        help="输入 JSON 文件路径；缺省或为 - 时从标准输入读取",
    )
    args = parser.parse_args(argv)

    if not args.payload or args.payload == "-":
        payload = json.load(sys.stdin)
    else:
        with open(args.payload, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

    try:
        output = run(payload)
    except InventoryValidationError as exc:
        json.dump(
            {"warehousable": False, "error_type": type(exc).__name__,
             "error": str(exc)},
            sys.stdout, ensure_ascii=False, indent=2,
        )
        sys.stdout.write("\n")
        return 2

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if output["warehousable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
