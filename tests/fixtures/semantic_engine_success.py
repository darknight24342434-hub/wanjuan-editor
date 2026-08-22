from __future__ import annotations

import json
import sys
from pathlib import Path


def annotation(
    annotation_id: str,
    function: str,
    block_id: str,
    questioned_annotation_id: str | None = None,
) -> dict:
    item = {
        "id": annotation_id,
        "function": function,
        "severity": "warning" if function in {"正確性", "指錯", "糾漏"} else "note",
        "message": f"{function}測試批註",
        "excerpt": "",
        "start": None,
        "end": None,
        "structure_block_id": block_id,
    }
    if questioned_annotation_id is not None or function in {"指錯", "糾漏", "建議"}:
        item["questioned_annotation_id"] = questioned_annotation_id
    return item


payload = json.loads(sys.stdin.read())
block_id = payload["structure"]["blocks"][-1]["id"]
if payload["role"] == "editor":
    output = {
        "annotations": [
            annotation(f"editor-{index}", function, block_id)
            for index, function in enumerate(("正確性", "可讀性", "文筆", "下標", "結構"))
        ],
        "headline_candidates": [
            {"id": "headline-a", "main_title": "河岸更新明年完工", "subtitle": "市府先改善步道與照明", "angle": "工程進度"},
            {"id": "headline-b", "main_title": "三億元河岸更新啟動", "subtitle": "居民要求施工保留通行", "angle": "預算與影響"},
        ],
    }
else:
    output = {
        "annotations": [
            annotation("chief-0", "指錯", block_id, "editor-0"),
            annotation("chief-1", "糾漏", block_id),
            annotation("chief-2", "建議", block_id),
        ],
        "headline_recommendation": {"text": "河岸更新啟動", "reason": "先寫最新事件"},
        "lead_recommendation": {"text": "市府今天宣布工程期程。", "reason": "導言先交代進度"},
        "angle_recommendation": {"text": "比較工程承諾與居民需求。", "reason": "切角具公共影響"},
    }
Path(sys.argv[1]).write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
