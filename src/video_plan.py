from __future__ import annotations

import re
from typing import Any


def _risk_item(item_id: str, label: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {
        "id": item_id,
        "label": label,
        "message": message,
        "severity": severity,
        "status": "human_check_required",
    }


def build_video_plan(
    title: str,
    content: str,
    source_notes: str,
    review_output: dict[str, Any],
    review_run_id: str,
) -> dict[str, Any]:
    """Build a traceable planning draft without pretending semantic approval."""
    clean_title = title.strip() or "未命名題目"
    clean_content = content.strip()
    if not clean_content:
        raise ValueError("文件沒有正文，無法建立影音企劃")

    characters = len(re.sub(r"\s", "", clean_content))
    source_haystack = f"{clean_content}\n{source_notes}"
    has_source_marker = bool(
        re.search(r"根據|表示|指出|聲明|公告|報導|資料|來源|採訪|https?://", source_haystack, re.I)
    )
    has_numbers = bool(re.search(r"\d", clean_content))
    has_allegation = bool(re.search(r"批|轟|控|指控|質疑|痛批|怒斥", clean_content))
    has_response = bool(re.search(r"回應|說明|尚未回覆|聯繫|求證", source_haystack))

    risks: list[dict[str, str]] = [
        _risk_item("semantic_review", "語義總編審", "百鬼質疑審與成品審尚未接入語義模型，不能視為已通過。"),
        _risk_item("human_approval", "人類核准", "影音角度、採攝規模與最終派工仍須由真人主管核定。"),
        _risk_item("rights", "素材權利", "使用第三方畫面、照片、音樂、截圖前，須確認授權、合理使用與標示方式。"),
    ]
    if not has_source_marker:
        risks.append(_risk_item("source", "消息來源", "尚未找到明顯來源或歸因線索；派工前須補上第一手來源與查證狀態。"))
    if has_numbers:
        risks.append(_risk_item("numbers", "數字查核", "稿件含數字；字幕、口播與圖卡須逐項核對數值、單位、期間及來源。", "note"))
    if has_allegation and not has_response:
        risks.append(_risk_item("response", "同項回應", "稿件含指控或攻防語句，尚未找到同項回應／求證狀態。"))
    if characters < 80:
        risks.append(_risk_item("material", "素材完整度", "正文資訊量過低，只能建立空白任務骨架，不宜進入拍攝。"))

    review_warnings = int(review_output.get("summary", {}).get("warnings", 0))
    review_notes = int(review_output.get("summary", {}).get("notes", 0))
    return {
        "engine": "local_rules",
        "action": "video_plan_draft",
        "notice": "已建立可編輯的影音企劃骨架；未呼叫語義模型，也不代表總編核准或可直接派工。",
        "status": {
            "code": "draft_not_dispatchable",
            "label": "企劃草稿・不可派工",
            "reason": "新聞編輯預檢已完成，但百鬼語義總編審與人類核准尚未完成。",
        },
        "provenance": {
            "review_run_id": review_run_id,
            "source_notes_present": bool(source_notes.strip()),
            "characters": characters,
        },
        "summary": {
            "warnings": sum(1 for item in risks if item["severity"] == "warning"),
            "notes": sum(1 for item in risks if item["severity"] == "note"),
            "total": len(risks),
            "review_warnings": review_warnings,
            "review_notes": review_notes,
        },
        "editorial_assessment": {
            "story": clean_title,
            "decision": "待真人總監判斷影音價值",
            "questions": [
                "這題為什麼現在值得做？",
                "主要觀眾是誰，他們看完要理解或採取什麼行動？",
                "現場是否有足以支撐影音的角色、動作、環境或文件？",
                "應做長影音、短影音、直播，還是只保留文字？",
            ],
        },
        "production_brief": {
            "core_question": f"這則「{clean_title}」最需要觀眾理解的核心問題是什麼？",
            "interview_targets": [
                "主要當事人或第一手來源",
                "直接受影響者",
                "能補充制度、數據或背景的專業者",
            ],
            "interview_questions": [
                "目前可以確認的事實與時間線是什麼？",
                "爭點雙方各自提出了哪些可查證主張？",
                "這件事對具體人物或群體造成什麼影響？",
                "被質疑方如何回應同一項問題？",
                "下一個可驗證的進展或觀察點是什麼？",
            ],
            "must_shots": [
                "交代人物與事件所在環境的建立鏡頭",
                "人物正在進行相關行動的中景與特寫",
                "可查證文件、數據或物件的細節畫面",
                "關鍵訪談金句與足夠前後文",
                "同時保留 16:9 橫式及 9:16 直式安全構圖",
            ],
            "b_roll": [
                "事件流程與場域細節",
                "受影響者的具體生活或工作情境",
                "已確認有權使用的資料畫面與圖表",
                "能銜接段落、避免全程 talking head 的過場",
            ],
            "delivery_checklist": [
                "責任人與交件時間",
                "橫式母版與直式素材",
                "乾淨人聲、環境音與備援音軌",
                "人名、職稱、數字、日期及字幕查核者",
            ],
        },
        "platform_versions": [
            {
                "name": "30 秒",
                "purpose": "單一爆點與發現新受眾",
                "structure": ["0–2 秒真實鉤子", "一句主張", "一個證據", "清楚收束或導回母內容"],
            },
            {
                "name": "60 秒",
                "purpose": "Reels／Shorts 的事件完整版本",
                "structure": ["鉤子", "必要背景", "關鍵金句", "一個反差或影響", "下一步"],
            },
            {
                "name": "90 秒",
                "purpose": "Facebook與官網嵌入的脈絡版本",
                "structure": ["事件與重要性", "第二項證據", "同項回應或反方", "影響", "後續觀察"],
            },
            {
                "name": "YouTube 長版",
                "purpose": "只有在來源、人物與畫面足以支撐時啟用",
                "structure": ["承諾清楚的開場", "人物／衝突", "證據與背景", "回應與反例", "Payoff與下一步"],
            },
        ],
        "risk_gate": risks,
        "next_steps": [
            "完成百鬼總編質疑審與成品審。",
            "由真人主管選定目標觀眾、平台、成品長度與投入規模。",
            "補齊採訪對象、責任人、交件時間、素材權利與查核者。",
            "核准後再建立正式派工卡；本草稿不得直接對外發布。",
        ],
    }
