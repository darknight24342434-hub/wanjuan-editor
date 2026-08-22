from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml


ASSET_KINDS = {
    "style_cards": "style_cards",
    "templates": "templates",
    "editors": "editors",
}


RECOMMENDATION_ROUTES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("政治", "時事", "政黨", "政策"), ("style.genre.taiwan_political_sour_column", "style.genre.cold_satirical_column", "style.genre.animal_farm_political_fable"), "政治與公共議題"),
    (("毒舌", "諷刺", "酸", "吐槽"), ("style.genre.taiwan_poison_short", "style.genre.wilde_epigram", "style.genre.lu_xun_short_sting"), "銳利諷刺"),
    (("職場", "主管", "會議", "kpi", "官腔"), ("style.genre.workplace_absurdism", "style.genre.business_jargon_translator", "style.genre.taiwan_poison_short"), "職場荒謬"),
    (("銷售", "成交", "顧問", "服務", "提案"), ("style.genre.high_ticket_consulting", "style.genre.ad_short_blade", "style.copy.en.david_ogilvy"), "商業說服"),
    (("品牌", "創辦人", "信任", "故事"), ("style.genre.founder_confession", "style.genre.warm_story", "style.genre.knowledge_talk"), "品牌與人物故事"),
    (("溫暖", "療癒", "感人", "人情"), ("style.genre.warm_story", "style.film_screenwriter.tw.wu_nien_jen", "style.literary.ru.anton_chekhov"), "低糖溫暖敘事"),
    (("知識", "科普", "歷史", "說書", "教學"), ("style.genre.knowledge_talk", "style.literary.en.george_orwell", "style.literary.fr.albert_camus"), "知識說明"),
    (("武俠", "江湖", "門派"), ("style.fiction.zh.jin_yong", "style.fiction.zh.gu_long", "style.fiction.zh.lao_she"), "武俠敘事"),
    (("推理", "犯罪", "案件", "偵探"), ("style.fiction.jp.matsumoto_seicho", "style.fiction.en.raymond_chandler", "style.fiction.en.patricia_highsmith"), "推理與犯罪"),
    (("科幻", "未來", "宇宙", "文明"), ("style.fiction.zh.liu_cixin", "style.fiction.en.ursula_k_le_guin", "style.fiction.en.philip_k_dick"), "科幻與文明命題"),
    (("孤獨", "城市", "留白", "感情"), ("style.fiction.jp.murakami_haruki", "style.film_screenwriter.zh.wang_jiawei", "style.literary.zh.zhang_ailing"), "城市、關係與留白"),
)


class CatalogError(RuntimeError):
    """卡庫索引或資產不符合唯讀契約。"""


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _section_paragraph(text: str, headings: tuple[str, ...]) -> str:
    lines = text.splitlines()
    for heading in headings:
        try:
            index = next(i for i, line in enumerate(lines) if line.strip() == heading)
        except StopIteration:
            continue
        paragraph: list[str] = []
        for line in lines[index + 1 :]:
            stripped = line.strip()
            if stripped.startswith("##"):
                break
            if not stripped:
                if paragraph:
                    break
                continue
            if stripped.startswith(("- ", "```")):
                continue
            paragraph.append(stripped)
        if paragraph:
            return " ".join(paragraph)[:240]
    return ""


def _section_bullets(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    try:
        index = next(i for i, line in enumerate(lines) if line.strip().casefold() == heading.casefold())
    except StopIteration:
        return []
    bullets: list[str] = []
    for line in lines[index + 1 :]:
        stripped = line.strip()
        if stripped.startswith("##"):
            break
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
        if len(bullets) >= 3:
            break
    return bullets


def _training_tier(status: str, family: str) -> str:
    if family != "literary_master":
        return "not_applicable"
    normalized = status.lower()
    if "20plus_close_read_complete" in normalized:
        return "complete_text_20plus"
    if any(
        key in normalized
        for key in (
            "original_language_10plus_formal_complete",
            "original_language_near20_formal_complete",
            "near20_formal_complete",
            "distinct19_user_accepted_complete",
        )
    ):
        return "transparent_10plus_near20"
    if "translation_assisted" in normalized:
        return "translation_boundary"
    if any(key in normalized for key in ("partial", "machine_pass", "not10plus", "not20", "distinct")):
        return "partial_source"
    return "legacy_or_regular"


def _qualification(status: str, family: str) -> tuple[str, str, str, str]:
    tier = _training_tier(status, family)
    if tier == "complete_text_20plus":
        return (
            "完整文本書目・20+ 原文細讀",
            "complete",
            tier,
            "INDEX 與卡片狀態證明為 original_language_20plus_close_read_complete。20+ 指可驗證作者原語文本或作品單位，不保證每一項都是整本書。",
        )
    if tier == "transparent_10plus_near20":
        return (
            "10+／近 20 透明可用",
            "transparent",
            tier,
            "具 10+ 或近 20 的透明來源基礎，可調用，但不得稱為完整文本 20+ 原文細讀卡。",
        )
    if tier == "translation_boundary":
        return (
            "譯本輔助／邊界限制",
            "boundary",
            tier,
            "只能取敘事機關與結構旁證，不得宣稱作者原語句法或完整文本訓練。",
        )
    if tier == "partial_source":
        return (
            "部分文本／機器檢查",
            "partial",
            tier,
            "來源或細讀尚未達新制完整門檻，只能透明限用。",
        )
    if tier == "legacy_or_regular":
        return (
            "舊制／一般可用・未證明完整文本",
            "active",
            tier,
            "可使用，但目前沒有新制完整文本 20+ 原文細讀資格證明。",
        )
    return "正式可用", "active", tier, "此卡不屬文學大師書目訓練分級。"


class BaiguiCatalog:
    """以 INDEX.yaml 為唯一清單來源的唯讀卡庫轉接器。"""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.index_path = (self.root / "INDEX.yaml").resolve()
        self._assets: dict[str, list[dict[str, Any]]] = {}
        self._by_id: dict[str, tuple[str, dict[str, Any]]] = {}
        self.updated = ""
        self.reload()

    def reload(self) -> None:
        if not self.index_path.is_file():
            raise CatalogError(f"找不到百鬼索引：{self.index_path}")
        try:
            raw = yaml.safe_load(self.index_path.read_text(encoding="utf-8-sig")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise CatalogError(f"無法解析百鬼索引：{exc}") from exc

        active = raw.get("active_assets")
        if not isinstance(active, dict):
            raise CatalogError("INDEX.yaml 缺少 active_assets")

        assets: dict[str, list[dict[str, Any]]] = {}
        by_id: dict[str, tuple[str, dict[str, Any]]] = {}
        for public_kind, index_key in ASSET_KINDS.items():
            rows = active.get(index_key, [])
            if not isinstance(rows, list):
                raise CatalogError(f"active_assets.{index_key} 不是清單")
            normalized_rows: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id") or not row.get("path"):
                    continue
                asset_path = self._safe_asset_path(str(row["path"]))
                if not asset_path.is_file():
                    continue
                text = asset_path.read_text(encoding="utf-8-sig", errors="replace")
                frontmatter = _parse_frontmatter(text)
                effective_status = str(frontmatter.get("status") or row.get("status") or "active")
                family = str(row.get("family") or frontmatter.get("family") or frontmatter.get("kind") or "")
                quality_label, quality_tone, training_tier, training_note = _qualification(effective_status, family)
                normalized = {
                    **row,
                    "kind": public_kind,
                    "status": effective_status,
                    "family": family,
                    "role": frontmatter.get("role") or "",
                    "style_axis": frontmatter.get("style_axis") or "",
                    "best_for": frontmatter.get("best_for") or [],
                    "tags": frontmatter.get("tags") or [],
                    "temperature": frontmatter.get("temperature") or "",
                    "region": frontmatter.get("region") or "",
                    "plain_direction": _section_paragraph(text, ("## 核心手感", "## 調用提示", "## 一句定魂")),
                    "avoid": _section_bullets(text, "## Avoid"),
                    "quality_label": quality_label,
                    "quality_tone": quality_tone,
                    "training_tier": training_tier,
                    "training_note": training_note,
                    "complete_text_training": training_tier == "complete_text_20plus",
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
                asset_id = str(normalized["id"])
                if asset_id in by_id:
                    raise CatalogError(f"重複資產 ID：{asset_id}")
                normalized_rows.append(normalized)
                by_id[asset_id] = (public_kind, normalized)
            assets[public_kind] = normalized_rows

        self._assets = assets
        self._by_id = by_id
        self.updated = str(raw.get("updated") or "")

    def _safe_asset_path(self, relative_path: str) -> Path:
        normalized = relative_path.replace("/", "\\")
        candidate = (self.root / normalized).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise CatalogError(f"資產路徑越界：{relative_path}") from exc
        if "00_admin" in candidate.parts or "_draft_missing_qcpass_20260617" in candidate.parts:
            raise CatalogError(f"禁止讀取非 runtime 資產：{relative_path}")
        return candidate

    def counts(self) -> dict[str, int]:
        return {kind: len(rows) for kind, rows in self._assets.items()}

    def families(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for item in self._assets.get("style_cards", []):
            family = str(item.get("family") or "未分類")
            counts[family] = counts.get(family, 0) + 1
        return [{"id": key, "count": counts[key]} for key in sorted(counts)]

    def training_summary(self) -> dict[str, int]:
        style_cards = self._assets.get("style_cards", [])
        literary = [item for item in style_cards if item.get("family") == "literary_master"]
        complete = [item for item in literary if item.get("complete_text_training")]
        return {
            "all_style_cards": len(style_cards),
            "literary_masters": len(literary),
            "complete_text_20plus": len(complete),
            "other_literary_masters": len(literary) - len(complete),
        }

    def list_assets(self, kind: str, query: str = "", family: str = "") -> list[dict[str, Any]]:
        if kind not in ASSET_KINDS:
            raise CatalogError(f"不支援的資產類型：{kind}")
        needle = query.strip().casefold()
        results = []
        for item in self._assets[kind]:
            if family and str(item.get("family") or "") != family:
                continue
            haystack = " ".join(
                str(item.get(field) or "")
                for field in ("id", "name", "family", "style_axis", "role", "quality_label")
            ).casefold()
            if needle and needle not in haystack:
                continue
            results.append({k: v for k, v in item.items() if k != "content"})
        return results

    def get_asset(self, kind: str, asset_id: str) -> dict[str, Any]:
        found = self._by_id.get(asset_id)
        if not found or found[0] != kind:
            raise CatalogError(f"找不到資產：{asset_id}")
        item = found[1]
        path = self._safe_asset_path(str(item["path"]))
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        return {**item, "content": content}

    def has_asset(self, kind: str, asset_id: str | None) -> bool:
        if not asset_id:
            return True
        found = self._by_id.get(asset_id)
        return bool(found and found[0] == kind)

    def recommend_style_cards(self, concept: str, limit: int = 5) -> list[dict[str, Any]]:
        clean = concept.strip().casefold()
        if not clean:
            raise CatalogError("請先輸入寫作概念")
        scores: dict[str, int] = {}
        reasons: dict[str, list[str]] = {}
        for keywords, asset_ids, label in RECOMMENDATION_ROUTES:
            matched = [word for word in keywords if word.casefold() in clean]
            if not matched:
                continue
            for rank, asset_id in enumerate(asset_ids):
                scores[asset_id] = scores.get(asset_id, 0) + 30 - rank * 4 + len(matched)
                reasons.setdefault(asset_id, []).append(f"符合「{label}」方向")

        for item in self._assets.get("style_cards", []):
            asset_id = str(item["id"])
            name = str(item.get("name") or "").casefold()
            if len(name) >= 2 and name in clean:
                scores[asset_id] = scores.get(asset_id, 0) + 50
                reasons.setdefault(asset_id, []).append("概念直接點名這張卡")

        if not scores:
            for rank, asset_id in enumerate(("style.genre.knowledge_talk", "style.genre.warm_story", "style.genre.taiwan_poison_short")):
                scores[asset_id] = 10 - rank
                reasons[asset_id] = ["概念尚未形成明確風格，先提供差異較大的起始方向"]

        ranked = sorted(scores, key=lambda asset_id: (-scores[asset_id], asset_id))
        results: list[dict[str, Any]] = []
        for asset_id in ranked:
            found = self._by_id.get(asset_id)
            if not found or found[0] != "style_cards":
                continue
            item = found[1]
            best_for = item.get("best_for") or []
            direction = item.get("plain_direction") or item.get("style_axis") or "請查看卡片正本判斷文字取向。"
            avoid = item.get("avoid") or []
            results.append(
                {
                    "id": asset_id,
                    "name": item.get("name"),
                    "family": item.get("family"),
                    "direction": direction,
                    "why": "；".join(reasons.get(asset_id, [])),
                    "best_for": best_for[:4] if isinstance(best_for, list) else [str(best_for)],
                    "avoid": avoid,
                    "quality_label": item.get("quality_label"),
                    "training_note": item.get("training_note"),
                }
            )
            if len(results) >= max(1, min(limit, 8)):
                break
        return results
