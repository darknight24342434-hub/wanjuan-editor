from __future__ import annotations

import base64
import binascii
import io
import re
import zipfile
from pathlib import PurePath
from xml.etree import ElementTree


MAX_DECODED_BYTES = 2 * 1024 * 1024
DOCX_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _safe_name(filename: str) -> str:
    return PurePath(filename.replace("\\", "/")).name[:180]


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "big5", "cp950"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("DOCX 檔案損壞或缺少正文") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError("DOCX 正文 XML 無法解析") from exc
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{DOCX_NAMESPACE}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{DOCX_NAMESPACE}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{DOCX_NAMESPACE}tab":
                parts.append("\t")
            elif node.tag == f"{DOCX_NAMESPACE}br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def import_document(filename: str, encoded_content: str) -> dict[str, str | int]:
    safe_name = _safe_name(filename)
    if not safe_name:
        raise ValueError("缺少檔名")
    try:
        data = base64.b64decode(encoded_content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("稿件內容不是有效 Base64") from exc
    if len(data) > MAX_DECODED_BYTES:
        raise ValueError("稿件解碼後超過 2 MB")
    suffix = PurePath(safe_name).suffix.casefold()
    if suffix == ".docx":
        content = _read_docx(data)
    elif suffix in {".txt", ".md", ".markdown"}:
        content = _decode_text(data)
    else:
        raise ValueError("目前只支援 TXT、MD 與 DOCX")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        raise ValueError("稿件沒有可讀文字")
    title = re.sub(r"[_-]+", " ", PurePath(safe_name).stem).strip() or "匯入稿件"
    return {"filename": safe_name, "title": title, "content": content, "characters": len(re.sub(r"\s", "", content))}
