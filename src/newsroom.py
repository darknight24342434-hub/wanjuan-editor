from __future__ import annotations

import hashlib
import re
from typing import Any


SOURCE_STATUSES = {"pending", "confirmed", "doubt", "gap"}
REWRITE_PERSONAS = {
    "editor.news": "新聞編輯卡",
    "editor.baigui_editor_in_chief": "百鬼總編卡",
    "editor.de_ai": "去AI味編輯",
}

AI_REPLACEMENTS = {
    "賦能": "提供支援",
    "打造": "建立",
    "關鍵": "主要",
    "深入探討": "說明",
    "提升效率": "縮短處理時間",
    "全方位": "各項",
    "生態系": "協作體系",
    "價值創造": "實際成果",
    "核心競爭力": "主要優勢",
    "在這個快速變化的時代": "目前",
}


def _clue_id(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}\0{text}".encode("utf-8")).hexdigest()[:16]


def extract_source_clues(content: str, imported_hint: str = "") -> list[dict[str, str]]:
    clues: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, text: str) -> None:
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return
        key = f"{kind}\0{clean}"
        if key in seen:
            return
        seen.add(key)
        clues.append({"clue_id": _clue_id(kind, clean), "clue_kind": kind, "clue_text": clean})

    if imported_hint.strip():
        add("attachment", imported_hint)
    for url in re.findall(r"https?://[^\s)）\]】>]+", content, re.I):
        add("url", url)
    for line in content.splitlines():
        clean = line.strip()
        if re.match(r"^(來源|資料來源|記者|編譯|攝影|圖|文|採訪)\s*[：:]", clean):
            add("byline", clean)
    for sentence in re.split(r"(?<=[。！？!?])\s*", content):
        clean = sentence.strip()
        if clean and len(clean) <= 260 and re.search(r"根據|表示|指出|聲明|公告|報導|告訴|說法|資料顯示|受訪", clean):
            add("attribution", clean)
    if not clues:
        add("missing", "稿件尚未辨識到可核對的來源線索")
    return clues[:20]


def merge_source_decisions(
    clues: list[dict[str, str]],
    stored: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stored_by_id = {item["clue_id"]: item for item in stored}
    merged = []
    for clue in clues:
        saved = stored_by_id.get(clue["clue_id"], {})
        status = str(saved.get("status") or "pending")
        if status not in SOURCE_STATUSES:
            status = "pending"
        merged.append(
            {
                **clue,
                "status": status,
                "note": str(saved.get("note") or ""),
                "updated_at": saved.get("updated_at"),
            }
        )
    return merged


def source_readiness(clues: list[dict[str, Any]]) -> dict[str, Any]:
    missing_markers = [item for item in clues if item.get("clue_kind") == "missing"]
    pending = [item for item in clues if item["status"] == "pending"]
    gaps = [item for item in clues if item["status"] == "gap"]
    doubts_without_note = [
        item for item in clues if item["status"] == "doubt" and not str(item.get("note") or "").strip()
    ]
    ready = bool(clues) and not missing_markers and not pending and not gaps and not doubts_without_note
    blockers = []
    if missing_markers:
        blockers.append("稿件沒有可核對的來源線索，必須補入來源後重新儲存")
    if pending:
        blockers.append(f"{len(pending)} 項來源線索尚未裁定")
    if gaps:
        blockers.append(f"{len(gaps)} 項來源缺口尚未補齊")
    if doubts_without_note:
        blockers.append(f"{len(doubts_without_note)} 項存疑來源未留下說明")
    return {
        "ready": ready,
        "missing_markers": len(missing_markers),
        "pending": len(pending),
        "gaps": len(gaps),
        "doubts_without_note": len(doubts_without_note),
        "blockers": blockers,
    }


def rewrite_with_persona(content: str, persona_id: str) -> dict[str, Any]:
    if persona_id not in REWRITE_PERSONAS:
        raise ValueError("不支援的改寫人物卡")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    normalized = re.sub(r"[!?！？]{2,}", lambda match: match.group(0)[0], normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    changes: list[str] = ["整理空白、標點與段落格式"]

    if persona_id in {"editor.news", "editor.baigui_editor_in_chief"}:
        normalized = re.sub(
            r"^(來源|資料來源|記者|編譯|攝影|採訪)\s*:",
            lambda match: f"{match.group(1)}：",
            normalized,
            flags=re.M,
        )
        paragraphs = []
        for paragraph in normalized.split("\n\n"):
            compact = paragraph.strip()
            if len(compact) > 60:
                sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])", compact) if part.strip()]
                if len(sentences) > 1:
                    midpoint = max(1, len(sentences) // 2)
                    paragraphs.extend(["".join(sentences[:midpoint]), "".join(sentences[midpoint:])])
                    continue
            paragraphs.append(compact)
        normalized = "\n\n".join(part for part in paragraphs if part)
        changes.append("統一新聞署名格式，必要時拆分過長段落")

    if persona_id == "editor.de_ai":
        replaced = []
        for phrase, replacement in AI_REPLACEMENTS.items():
            if phrase in normalized:
                normalized = normalized.replace(phrase, replacement)
                replaced.append(phrase)
        changes.append("替換空泛套語" if replaced else "未發現需替換的高風險套語")

    return {
        "persona_id": persona_id,
        "persona_name": REWRITE_PERSONAS[persona_id],
        "content": normalized,
        "changes": changes,
        "changed": normalized != content,
        "notice": "本次為本機保守改寫；不新增事實、不替代來源查核或語義總編審。",
    }
