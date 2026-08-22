from __future__ import annotations

import re
import uuid
from typing import Any


AI_PHRASES = (
    "賦能",
    "打造",
    "關鍵",
    "深入探討",
    "提升效率",
    "全方位",
    "生態系",
    "價值創造",
    "核心競爭力",
    "在這個快速變化的時代",
)


def _suggestion(
    category: str,
    severity: str,
    message: str,
    excerpt: str = "",
    start: int | None = None,
    end: int | None = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "category": category,
        "severity": severity,
        "message": message,
        "excerpt": excerpt,
        "start": start,
        "end": end,
    }


def review_content(title: str, content: str, workflow_id: str = "general", source_notes: str = "") -> dict[str, Any]:
    suggestions: list[dict[str, Any]] = []
    if not title.strip() or title.strip() == "未命名文件":
        suggestions.append(_suggestion("標題", "warning", "標題尚未定稿，匯出或送審前應補上。"))

    for match in re.finditer(r"[!?！？]{2,}", content):
        suggestions.append(
            _suggestion(
                "標點",
                "warning",
                "連續驚嘆或問號會削弱語氣，建議保留一個。",
                match.group(0),
                match.start(),
                match.end(),
            )
        )

    for match in re.finditer(r" {2,}", content):
        suggestions.append(
            _suggestion(
                "格式",
                "note",
                "發現連續空白，建議整理格式。",
                match.group(0),
                match.start(),
                match.end(),
            )
        )

    for phrase in AI_PHRASES:
        start = 0
        while True:
            found = content.find(phrase, start)
            if found < 0:
                break
            suggestions.append(
                _suggestion(
                    "AI 痕跡",
                    "warning",
                    f"「{phrase}」屬百鬼禁用／高風險套語，請改成具體動作或事實。",
                    phrase,
                    found,
                    found + len(phrase),
                )
            )
            start = found + len(phrase)

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
    non_space_chars = len(re.sub(r"\s", "", content))
    for paragraph in paragraphs:
        compact = re.sub(r"\s+", "", paragraph)
        if len(compact) > 280:
            start = content.find(paragraph)
            suggestions.append(
                _suggestion(
                    "節奏",
                    "note",
                    "這個段落超過 280 字，建議檢查是否能拆成兩個論點或場景。",
                    compact[:40] + "…",
                    start if start >= 0 else None,
                    start + len(paragraph) if start >= 0 else None,
                )
            )

    rewrite_personas: list[dict[str, str]] = []
    workflow: dict[str, Any] | None = None
    if workflow_id == "chinatimes_newsroom":
        if len(title.strip()) > 44:
            suggestions.append(
                _suggestion("新聞標題", "note", "標題超過近期中時公開語料觀察到的 44 字上界；請檢查是否能縮短，但這不是硬性官方規則。", title.strip())
            )
        if non_space_chars < 80:
            suggestions.append(_suggestion("稿件完整度", "warning", "正文資訊量過低，尚不足以進入完整新聞審稿。"))
        first_paragraph = paragraphs[0] if paragraphs else ""
        if len(re.sub(r"\s", "", first_paragraph)) > 180:
            suggestions.append(_suggestion("導言", "note", "第一段負擔過重；請先交代最新事件及其重要性，再移出背景。", first_paragraph[:50] + "…"))
        source_haystack = f"{content}\n{source_notes}"
        if non_space_chars >= 80 and not re.search(r"根據|表示|指出|聲明|公告|報導|告訴|說法|資料|來源|採訪|https?://", source_haystack, re.I):
            suggestions.append(_suggestion("消息來源", "warning", "未找到明顯來源或歸因線索；新聞編輯卡應先標出每項中央主張的來源定位。"))
        if re.search(r"\d", content):
            suggestions.append(_suggestion("數字查核", "note", "稿件含數字；送總編前應逐項核對數值、單位、期間與來源定位。"))
        if re.search(r"批|轟|控|指控|質疑|痛批|怒斥", content) and not re.search(r"回應|說明|尚未回覆|聯繫|求證", content):
            suggestions.append(_suggestion("同項回應", "warning", "稿件含攻防或指控語句，但未找到回應／求證狀態；需確認是否回應同一項指控。"))

        rewrite_personas.extend(
            [
                {"id": "editor.news", "name": "新聞編輯卡", "reason": "負責標題、錯字、專名、數字、引文、分桌與結構改寫。"},
                {"id": "editor.baigui_editor_in_chief", "name": "百鬼總編卡", "reason": "先質疑來源與判斷，再檢查可讀性、有趣性與深度。"},
            ]
        )
        if any(item["category"] in {"AI 痕跡", "節奏", "格式", "標點"} for item in suggestions):
            rewrite_personas.append({"id": "editor.de_ai", "name": "去AI味編輯", "reason": "稿件出現模板腔、節奏或句面問題，適合在事實與結構修正後收尾。"})
        workflow = {
            "id": "chinatimes_newsroom",
            "label": "中時新聞審稿",
            "stages": [
                {"name": "新聞編輯預檢", "status": "local_preflight_complete"},
                {"name": "百鬼總編質疑審", "status": "semantic_model_required"},
                {"name": "百鬼總編成品審", "status": "semantic_model_required"},
                {"name": "人類核准", "status": "required"},
            ],
        }

    sentence_count = len([s for s in re.split(r"[。！？!?]+", content) if s.strip()])
    severity_order = {"error": 0, "warning": 1, "note": 2}
    suggestions.sort(key=lambda item: severity_order.get(item["severity"], 9))
    return {
        "engine": "local_rules",
        "notice": "本次為本機規則檢查，未呼叫外部 AI，也不代表總編核准。" if workflow_id != "chinatimes_newsroom" else "已完成新聞本機預檢；來源質疑、可讀性、有趣性與深度仍等待語義模型，不能視為總編通過。",
        "stats": {
            "characters": non_space_chars,
            "paragraphs": len(paragraphs),
            "sentences": sentence_count,
            "estimated_reading_minutes": max(1, round(non_space_chars / 500)) if non_space_chars else 0,
        },
        "summary": {
            "warnings": sum(1 for item in suggestions if item["severity"] == "warning"),
            "notes": sum(1 for item in suggestions if item["severity"] == "note"),
            "total": len(suggestions),
        },
        "suggestions": suggestions,
        "workflow": workflow,
        "rewrite_personas": rewrite_personas,
    }
