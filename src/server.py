from __future__ import annotations

import argparse
import difflib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from .catalog import BaiguiCatalog, CatalogError
from .importer import import_document
from .media_pipeline import MediaFailure, MediaJobManager, MediaPipeline
from .newsroom import (
    REWRITE_PERSONAS,
    SOURCE_STATUSES,
    extract_source_clues,
    merge_source_decisions,
    rewrite_with_persona,
    source_readiness,
)
from .repository import DocumentRepository
from .review import review_content
from .semantic import (
    SemanticEngineAdapter,
    SemanticFailure,
    SemanticJobManager,
    json_hash,
    load_engine_config,
)
from .structure import StructureError, apply_structure_overrides, detect_structure
from .video_plan import build_video_plan
from .writer import WriterCardRegistry, WriterEngineAdapter, WriterFailure, WriterJobManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 兩個外部語料庫的位置。原本寫死成兩個絕對路徑，那兩個路徑連在原機器上都已經
# 不存在了。改用環境變數；沒設時指向 repo 底下一個不存在的位置，讓相關功能照原
# 本的方式回報「找不到來源」，而不是在啟動時就爆掉。
DEFAULT_BAIGUI_ROOT = Path(
    os.environ.get("WENKU_BAIGUI_ROOT") or (PROJECT_ROOT / "external" / "baigui-library")
)
DEFAULT_CHINATIMES_ROOT = Path(
    os.environ.get("WENKU_CHINATIMES_ROOT") or (PROJECT_ROOT / "external" / "news-corpus")
)
DEFAULT_ACCESS_CONFIG = PROJECT_ROOT / "data" / "access.json"
MAX_BODY_BYTES = 2 * 1024 * 1024
LOCAL_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOCAL_CLIENT_ADDRESSES = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
SESSION_COOKIE_NAME = "wenku_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_SECONDS = 10 * 60
PUBLIC_PATHS = frozenset({"/login", "/login.css", "/login.js", "/favicon.svg", "/api/login"})
LOCAL_ADDRESS_CACHE_SECONDS = 60.0

_local_address_cache: tuple[float, frozenset[str]] = (0.0, frozenset())


class WorkflowConflictError(ValueError):
    """The client acted on a workflow snapshot that is no longer current."""


class AccessConfig:
    """data/access.json：外部連線開關與登入密碼。"""

    def __init__(
        self,
        password: str = "",
        allow_external: bool = False,
        extra_hosts: Iterable[str] = (),
    ):
        self.password = password
        self.allow_external = allow_external
        self.extra_hosts = tuple(
            str(item).strip().lower() for item in extra_hosts if str(item).strip()
        )

    @property
    def external_enabled(self) -> bool:
        return bool(self.allow_external and self.password)


def load_access_config(path: Path) -> AccessConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return AccessConfig()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"存取設定讀取失敗：{path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ValueError("存取設定的根節點必須是物件")
    extra_hosts = raw.get("extra_hosts") or []
    if not isinstance(extra_hosts, list):
        raise ValueError("extra_hosts 必須是清單")
    return AccessConfig(
        str(raw.get("password") or ""),
        bool(raw.get("allow_external")),
        extra_hosts,
    )


class SessionStore:
    """記憶體 session：伺服器重啟即全部失效。"""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = time.time()
        for token in [key for key, expiry in self._tokens.items() if expiry <= now]:
            self._tokens.pop(token, None)

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._tokens[token] = time.time() + self.ttl_seconds
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            self._prune()
            return token in self._tokens


class LoginRateLimiter:
    """同一來源 IP 連續密碼錯誤達上限即暫時鎖定。"""

    def __init__(
        self,
        limit: int = LOGIN_FAILURE_LIMIT,
        lock_seconds: int = LOGIN_LOCK_SECONDS,
    ):
        self.limit = limit
        self.lock_seconds = lock_seconds
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_seconds(self, client: str) -> int:
        with self._lock:
            _, until = self._failures.get(client, (0, 0.0))
            remaining = until - time.time()
            if remaining <= 0:
                if until:
                    self._failures.pop(client, None)
                return 0
            return int(remaining) + 1

    def register_failure(self, client: str) -> int:
        with self._lock:
            count, until = self._failures.get(client, (0, 0.0))
            count += 1
            if count >= self.limit:
                until = time.time() + self.lock_seconds
            self._failures[client] = (count, until)
            return count

    def clear(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


def client_is_local(address: str) -> bool:
    return str(address).strip().lower() in LOCAL_CLIENT_ADDRESSES


def local_ip_addresses(refresh: bool = False) -> set[str]:
    """本機網卡位址（含 Tailscale 等虛擬網卡）；快取 60 秒避免每次請求查詢。"""
    global _local_address_cache
    stamp, cached = _local_address_cache
    now = time.time()
    if not refresh and cached and now - stamp < LOCAL_ADDRESS_CACHE_SECONDS:
        return set(cached)
    addresses = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, proto=socket.IPPROTO_TCP):
            address = str(info[4][0]).split("%", 1)[0].strip()
            if address:
                addresses.add(address)
    except OSError:
        pass
    _local_address_cache = (now, frozenset(addresses))
    return addresses


def server_host_names(port: int, addresses: Iterable[str] | None = None) -> set[str]:
    """把本機網卡位址展開成 Host 標頭可能的寫法。"""
    names: set[str] = set()
    for raw in local_ip_addresses() if addresses is None else addresses:
        address = str(raw).strip().lower()
        if not address:
            continue
        host = f"[{address}]" if ":" in address and not address.startswith("[") else address
        names.add(host)
        names.add(f"{host}:{port}")
    return names


def allowed_local_host(raw_host: str, port: int, extra_hosts: Iterable[str] | None = None) -> bool:
    host = raw_host.strip().lower()
    allowed = {
        "127.0.0.1",
        "localhost",
        "[::1]",
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
    }
    if extra_hosts:
        allowed.update(str(item).strip().lower() for item in extra_hosts if str(item).strip())
    return host in allowed


def mutation_request_error(
    headers: Any,
    port: int,
    extra_hosts: Iterable[str] | None = None,
) -> tuple[HTTPStatus, str] | None:
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "寫入 API 只接受 application/json"
    if str(headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return HTTPStatus.FORBIDDEN, "拒絕跨站寫入本機 API"
    origin = str(headers.get("Origin") or "").strip().lower()
    allowed_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }
    if extra_hosts:
        for item in extra_hosts:
            clean = str(item).strip().lower()
            if not clean:
                continue
            # 反向隧道（cloudflared）對外是 https，Origin 會是 https://<網域>
            allowed_origins.add(f"http://{clean}")
            allowed_origins.add(f"https://{clean}")
    if origin and origin not in allowed_origins:
        return HTTPStatus.FORBIDDEN, "Origin 不屬於本機文書房"
    return None


def media_mutation_request_error(
    headers: Any,
    port: int,
    extra_hosts: Iterable[str] | None = None,
) -> tuple[HTTPStatus, str] | None:
    """媒體上傳沿用 JSON API 的 CSRF／Origin 閘，但接受串流媒體內容型別。"""
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if not (
        content_type == "application/octet-stream"
        or content_type.startswith("video/")
        or content_type.startswith("audio/")
    ):
        return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "媒體上傳只接受 video/*、audio/* 或 application/octet-stream"
    if str(headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return HTTPStatus.FORBIDDEN, "拒絕跨站寫入本機 API"
    origin = str(headers.get("Origin") or "").strip().lower()
    allowed_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }
    if extra_hosts:
        for item in extra_hosts:
            clean = str(item).strip().lower()
            if clean:
                allowed_origins.update((f"http://{clean}", f"https://{clean}"))
    if origin and origin not in allowed_origins:
        return HTTPStatus.FORBIDDEN, "Origin 不屬於本機文書房"
    return None


class AppContext:
    def __init__(
        self,
        baigui_root: Path,
        database_path: Path,
        chinatimes_root: Path,
        engine_config_path: Path | None = None,
        access_config_path: Path | None = None,
        media_pipeline: MediaPipeline | None = None,
    ):
        self.catalog = BaiguiCatalog(baigui_root)
        self.repository = DocumentRepository(database_path)
        self.repository.ensure_welcome_document()
        self.chinatimes_root = chinatimes_root.resolve()
        self.access_config_path = Path(access_config_path or DEFAULT_ACCESS_CONFIG).resolve()
        self.access = load_access_config(self.access_config_path)
        self.sessions = SessionStore()
        self.login_guard = LoginRateLimiter()
        self.engine_config_path = (
            engine_config_path or PROJECT_ROOT / "data" / "engine.json"
        ).resolve()
        self.semantic_adapter = SemanticEngineAdapter(
            self.catalog,
            self.engine_config_path,
            PROJECT_ROOT,
            PROJECT_ROOT / "data" / ".semantic_tmp",
        )
        self.semantic_jobs = SemanticJobManager(self.repository, self.semantic_adapter)
        self.writer_registry = WriterCardRegistry(
            PROJECT_ROOT / "data" / "writer_cards.json",
            self.catalog.root,
        )
        self.writer_adapter = WriterEngineAdapter(PROJECT_ROOT, timeout_seconds=600)
        self.writer_jobs = WriterJobManager(
            self.repository,
            self.writer_registry,
            self.writer_adapter,
        )
        self.media_pipeline = media_pipeline or MediaPipeline(
            PROJECT_ROOT,
            media_root=Path(database_path).resolve().parent / "media",
        )
        self.media_jobs = MediaJobManager(self.media_pipeline)


def structure_state(context: AppContext, document_id: str) -> dict[str, Any]:
    document = context.repository.get_document(document_id)
    revision_id = document["current_revision_id"]
    detected = detect_structure(document["content"])
    latest_override = next(
        (
            item
            for item in context.repository.list_runs(document_id)
            if item["action"] == "structure_override"
            and item["revision_id"] == revision_id
            and item["status"] == "complete"
        ),
        None,
    )
    structure = detected
    if latest_override:
        candidate = latest_override.get("output") or {}
        candidate_blocks = candidate.get("blocks")
        detected_by_id = {item["id"]: item for item in detected["blocks"]}
        if isinstance(candidate_blocks, list) and len(candidate_blocks) == len(detected["blocks"]):
            valid = all(
                isinstance(item, dict)
                and item.get("id") in detected_by_id
                and item.get("text")
                == document["content"][int(item.get("start", -1)) : int(item.get("end", -1))]
                for item in candidate_blocks
            )
            if valid:
                structure = candidate
    result = {
        **structure,
        "document_id": document_id,
        "revision_id": revision_id,
        "structure_hash": json_hash(structure),
        "override_run_id": latest_override["id"] if latest_override else None,
    }
    return result


def semantic_engine_state(context: AppContext) -> dict[str, Any]:
    try:
        config = load_engine_config(context.engine_config_path)
        return {
            "status": "configured",
            "name": config.name,
            "timeout_seconds": config.timeout_seconds,
            "network_disclosure": config.network_disclosure,
        }
    except SemanticFailure as exc:
        return {"status": "unavailable", "error": exc.message}


def newsroom_state(context: AppContext, document_id: str) -> dict[str, Any]:
    document = context.repository.get_document(document_id)
    revision_id = document["current_revision_id"]
    clues = merge_source_decisions(
        extract_source_clues(
            document["content"],
            context.repository.get_source_hint(document_id, revision_id),
        ),
        context.repository.list_source_decisions(document_id, revision_id),
    )
    readiness = source_readiness(clues)
    runs = context.repository.list_runs(document_id)
    writer_run = None
    writer_skip = None
    revision_cursor = revision_id
    revisions_by_id = {
        item["id"]: item for item in context.repository.list_revisions(document_id)
    }
    while revision_cursor:
        writer_run = next(
            (
                item for item in runs
                if item["action"] == "writer_rewrite"
                and item["revision_id"] == revision_cursor
            ),
            None,
        )
        writer_skip = next(
            (
                item for item in runs
                if item["action"] == "writer_skip"
                and item["revision_id"] == revision_cursor
                and item["status"] == "complete"
            ),
            None,
        )
        if writer_run or writer_skip:
            break
        revision_cursor = str(
            (revisions_by_id.get(revision_cursor) or {}).get("parent_revision_id") or ""
        )
    writer_job = None
    writer_jobs = getattr(context, "writer_jobs", None)
    if writer_jobs is not None:
        writer_job = writer_jobs.latest(document_id, revision_id)
    if writer_job and writer_job["status"] in {"queued", "running"}:
        writer_status = writer_job["status"]
    elif writer_skip:
        writer_status = "skipped"
    elif writer_run:
        writer_status = writer_run["status"]
    else:
        writer_status = "not_started"
    writer_complete = bool(writer_run and writer_run["status"] == "complete")
    writer_allowed = writer_complete or bool(writer_skip)
    current_review = next(
        (
            item
            for item in runs
            if item["action"] == "local_review"
            and item["revision_id"] == revision_id
            and (item["output"].get("workflow") or {}).get("id") == "chinatimes_newsroom"
        ),
        None,
    )
    current_structure_hash = structure_state(context, document_id)["structure_hash"]
    semantic_runs = [
        item
        for item in runs
        if item["action"] == "semantic_review" and item["revision_id"] == revision_id
    ]
    current_semantic = next(
        (
            item
            for item in semantic_runs
            if (item.get("output") or {}).get("provenance", {}).get("structure_hash")
            == current_structure_hash
        ),
        semantic_runs[0] if semantic_runs else None,
    )
    semantic_job = None
    jobs = getattr(context, "semantic_jobs", None)
    if jobs is not None:
        semantic_job = jobs.latest(document_id, revision_id)
        if semantic_job and semantic_job.get("structure_hash") != current_structure_hash:
            semantic_job = None
    semantic_stale = bool(
        current_semantic
        and (current_semantic.get("output") or {}).get("provenance", {}).get("structure_hash")
        != current_structure_hash
    )
    if semantic_job and semantic_job["status"] in {"queued", "running"}:
        semantic_status = semantic_job["status"]
    elif current_semantic and semantic_stale:
        semantic_status = "stale"
    elif current_semantic:
        semantic_status = current_semantic["status"]
    else:
        semantic_status = "not_started"
    rewritten = writer_complete or str(document.get("actor") or "").startswith(("persona:", "writer:"))
    finalizable = rewritten or bool(writer_skip)
    approval_record = context.repository.get_approval(document_id, revision_id)
    approval_matches_review = bool(
        approval_record
        and current_review
        and approval_record["review_run_id"] == current_review["id"]
    )
    source_keys = ("clue_id", "clue_kind", "clue_text", "status", "note")
    current_source_snapshot = [
        {key: item.get(key) for key in source_keys}
        for item in clues
    ]
    approved_source_snapshot = [
        {key: item.get(key) for key in source_keys}
        for item in (approval_record or {}).get("source_snapshot", [])
    ]
    approval_matches_sources = bool(
        approval_record and current_source_snapshot == approved_source_snapshot
    )
    approval = approval_record if approval_matches_review and approval_matches_sources else None
    has_content = bool(document["content"].strip())
    if not has_content:
        stage = "intake"
    elif not writer_allowed:
        stage = "writer"
    elif semantic_status in {"queued", "running"}:
        stage = "chief" if semantic_job and semantic_job.get("pass") == "chief" else "editor"
    elif not (current_semantic and current_semantic["status"] == "complete" and not semantic_stale):
        stage = "editor"
    else:
        stage = "finalize"
    blockers = list(readiness["blockers"])
    if not current_review:
        blockers.append("目前版本尚未完成新聞審稿")
    if not writer_allowed:
        blockers.append("寫手尚未完成，且未明確跳過寫手")
    if not finalizable:
        blockers.append("目前版本尚未由人物卡建立修訂版本")
    if not approval:
        blockers.append("目前版本尚未人工核准")
    return {
        "document_id": document_id,
        "revision_id": revision_id,
        "stage": stage,
        "has_content": has_content,
        "sources": clues,
        "source_readiness": readiness,
        "writer": {
            "status": writer_status,
            "allowed_to_edit": writer_allowed,
            "complete": writer_complete,
            "skipped": bool(writer_skip),
            "job_id": writer_job["id"] if writer_job else None,
            "run_id": writer_run["id"] if writer_run else (writer_skip["id"] if writer_skip else None),
            "card_id": writer_run["card_id"] if writer_run else None,
            "actual_engine": (writer_run or {}).get("output", {}).get("provenance", {}).get("actual_engine"),
            "proxy": bool((writer_run or {}).get("output", {}).get("provenance", {}).get("proxy")),
            "proxy_label": (writer_run or {}).get("output", {}).get("provenance", {}).get("proxy_label", ""),
            "report": (writer_run or {}).get("output", {}).get("report"),
            "error": (writer_job or {}).get("error") or (writer_run or {}).get("output", {}).get("error"),
        },
        "review_current": bool(current_review),
        "review_run_id": current_review["id"] if current_review else None,
        "semantic": {
            "status": semantic_status,
            "job_id": semantic_job["id"] if semantic_job else None,
            "run_id": current_semantic["id"] if current_semantic else None,
            "complete": bool(current_semantic and current_semantic["status"] == "complete" and not semantic_stale),
            "stale": semantic_stale,
            "error": (current_semantic or {}).get("output", {}).get("error"),
        },
        "rewritten": rewritten,
        "finalizable": finalizable,
        "rewrite_actor": document.get("actor"),
        "approved": bool(approval),
        "approval": approval,
        "approval_stale": bool(approval_record and not approval),
        "video_plan_eligible": bool(approval and finalizable and current_review and readiness["ready"]),
        "blockers": blockers,
    }


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "BaiguiStudio/0.1"
    context: AppContext

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def _json(
        self,
        payload: Any,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: Iterable[tuple[str, str]] = (),
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _server_hosts(self) -> set[str]:
        """本機名單以外，另接受與伺服器實際監聽位址相符的 host[:port]。"""
        port = int(self.server.server_address[1])
        hosts = server_host_names(port)
        access = getattr(self.context, "access", None)
        if access is not None:
            for item in access.extra_hosts:
                hosts.add(item)
                if ":" not in item.rsplit("]", 1)[-1]:
                    hosts.add(f"{item}:{port}")
        return hosts

    def _client_address(self) -> str:
        return str(self.client_address[0]) if self.client_address else ""

    def _client_is_local(self) -> bool:
        return client_is_local(self._client_address())

    def _discard_small_request_body(self, maximum: int = MAX_BODY_BYTES) -> None:
        """拒絕 POST/PUT 時吸收已送達的小 body，避免 Windows TCP 以 RST 蓋掉錯誤回應。"""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            remaining = int(raw_length)
        except ValueError:
            return
        if remaining < 0 or remaining > maximum:
            self.close_connection = True
            return
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return ""
        morsel = jar.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else ""

    def _allow_local_request(self) -> bool:
        port = int(self.server.server_address[1])
        if allowed_local_host(str(self.headers.get("Host") or ""), port, self._server_hosts()):
            return True
        self._error(HTTPStatus.FORBIDDEN, "Host 不屬於本機文書房")
        return False

    def _allow_mutation_request(self) -> bool:
        issue = mutation_request_error(
            self.headers,
            int(self.server.server_address[1]),
            self._server_hosts(),
        )
        if issue is None:
            return True
        self._discard_small_request_body()
        self._error(*issue)
        return False

    def _allow_media_mutation_request(self) -> bool:
        issue = media_mutation_request_error(
            self.headers,
            int(self.server.server_address[1]),
            self._server_hosts(),
        )
        if issue is None:
            return True
        self._discard_small_request_body()
        self._error(*issue)
        return False

    def _allow_session_request(self, path: str) -> bool:
        """本機來源免登入；外部來源一律要帶有效 session cookie。

        免登入必須同時滿足「來源 IP 是本機」且「Host 是 localhost 名單」：
        反向隧道（如 cloudflared）代理的外部流量在本機端看起來也是 127.0.0.1，
        只看 IP 會讓外人整條免密碼；隧道流量帶的是公開網域 Host，靠這個分流。
        """
        port = int(self.server.server_address[1])
        if self._client_is_local() and allowed_local_host(
            str(self.headers.get("Host") or ""), port
        ):
            return True
        access = getattr(self.context, "access", None)
        if access is None or not access.external_enabled:
            self._discard_small_request_body()
            self._error(HTTPStatus.FORBIDDEN, "本服務未開放外部連線")
            return False
        if path in PUBLIC_PATHS:
            return True
        if self.context.sessions.valid(self._session_token()):
            return True
        if path.startswith("/api/"):
            self._discard_small_request_body()
            self._error(HTTPStatus.UNAUTHORIZED, "尚未登入或登入已逾時")
        else:
            self._redirect("/login")
        return False

    def _handle_login(self, data: dict[str, Any]) -> None:
        access = self.context.access
        if not access.external_enabled:
            self._error(HTTPStatus.FORBIDDEN, "本服務未開放外部連線，無需登入")
            return
        client = self._client_address()
        locked = self.context.login_guard.locked_seconds(client)
        if locked:
            self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"密碼連續錯誤已鎖定，請於 {math.ceil(locked / 60)} 分鐘後再試",
            )
            return
        password = str(data.get("password") or "")
        if not hmac.compare_digest(password.encode("utf-8"), access.password.encode("utf-8")):
            count = self.context.login_guard.register_failure(client)
            remaining = self.context.login_guard.limit - count
            if remaining > 0:
                message = f"密碼錯誤，還可再試 {remaining} 次"
            else:
                message = f"密碼錯誤次數過多，已鎖定 {self.context.login_guard.lock_seconds // 60} 分鐘"
            self._error(HTTPStatus.UNAUTHORIZED, message)
            return
        self.context.login_guard.clear(client)
        token = self.context.sessions.issue()
        cookie = (
            f"{SESSION_COOKIE_NAME}={token}; HttpOnly; SameSite=Lax; Path=/; "
            f"Max-Age={self.context.sessions.ttl_seconds}"
        )
        self._json({"status": "ok", "expires_in": self.context.sessions.ttl_seconds}, extra_headers=(("Set-Cookie", cookie),))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise ValueError("請求內容超過 2 MB")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON 格式錯誤") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON 根節點必須是物件")
        return data

    def _serve_static(self, relative: str) -> None:
        static_root = (PROJECT_ROOT / "static").resolve()
        requested = (static_root / relative).resolve()
        try:
            requested.relative_to(static_root)
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "找不到檔案")
            return
        if not requested.is_file():
            self._error(HTTPStatus.NOT_FOUND, "找不到檔案")
            return
        mime, _ = mimetypes.guess_type(requested.name)
        data = requested.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", (mime or "application/octet-stream") + ("; charset=utf-8" if mime and mime.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _upload_chunks(self, block_size: int = 1024 * 1024) -> Iterable[bytes]:
        """逐塊解讀固定長度或 HTTP/1.1 chunked body；永不做無界 read。"""
        transfer_encoding = str(self.headers.get("Transfer-Encoding") or "").strip().lower()
        if transfer_encoding:
            if transfer_encoding != "chunked":
                raise ValueError("不支援的 Transfer-Encoding")
            while True:
                line = self.rfile.readline(128)
                if not line or len(line) >= 128:
                    raise ValueError("chunked 上傳格式錯誤")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError as exc:
                    raise ValueError("chunked 上傳區塊大小錯誤") from exc
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(8192)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(block_size, remaining))
                    if not chunk:
                        raise ValueError("媒體上傳提前中斷")
                    remaining -= len(chunk)
                    yield chunk
                if self.rfile.read(2) != b"\r\n":
                    raise ValueError("chunked 上傳區塊結尾錯誤")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("媒體上傳需要 Content-Length 或 chunked 傳輸")
        try:
            remaining = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 格式錯誤") from exc
        if remaining < 0:
            raise ValueError("Content-Length 格式錯誤")
        while remaining:
            chunk = self.rfile.read(min(block_size, remaining))
            if not chunk:
                raise ValueError("媒體上傳提前中斷")
            remaining -= len(chunk)
            yield chunk

    def _serve_media_file(self, requested: Path, download_name: str | None = None) -> None:
        """以固定大小串流媒體，並支援瀏覽器單一 byte range。"""
        size = requested.stat().st_size
        start, end = 0, max(0, size - 1)
        status = HTTPStatus.OK
        raw_range = str(self.headers.get("Range") or "").strip()
        if raw_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range)
            if not match or "," in raw_range:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            left, right = match.groups()
            if left:
                start = int(left)
                end = int(right) if right else end
            elif right:
                suffix = int(right)
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = max(0, end - start + 1)
        mime, _ = mimetypes.guess_type(requested.name)
        self.send_response(status)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            safe_name = download_name.replace('"', "")
            self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.end_headers()
        with requested.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                data = handle.read(min(1024 * 1024, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._allow_local_request() or not self._allow_session_request(path):
            return
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                self._serve_static("index.html")
            elif path == "/login":
                self._serve_static("login.html")
            elif path in ("/app.js", "/styles.css", "/favicon.svg", "/login.css", "/login.js"):
                self._serve_static(path.lstrip("/"))
            elif path == "/api/health":
                self._json(
                    {
                        "status": "ok",
                        "mode": "local_private",
                        "catalog_updated": self.context.catalog.updated,
                        "counts": self.context.catalog.counts(),
                        "families": self.context.catalog.families(),
                        "training_summary": self.context.catalog.training_summary(),
                        "chinatimes_adapter": "newsroom_local_preflight",
                        "ai_engine": semantic_engine_state(self.context),
                    }
                )
            elif path == "/api/catalog":
                kind = query.get("kind", ["style_cards"])[0]
                text = query.get("q", [""])[0]
                family = query.get("family", [""])[0]
                self._json({"items": self.context.catalog.list_assets(kind, text, family)})
            elif path == "/api/catalog/item":
                kind = query.get("kind", [""])[0]
                asset_id = query.get("id", [""])[0]
                self._json(self.context.catalog.get_asset(kind, asset_id))
            elif path == "/api/documents":
                self._json({"items": self.context.repository.list_documents()})
            elif path == "/api/writer/cards":
                concept = query.get("concept", [""])[0]
                self._json(
                    {
                        "default_id": self.context.writer_registry.default_id,
                        "items": self.context.writer_registry.list_cards(concept),
                    }
                )
            elif match := re.fullmatch(r"/api/writer/jobs/([^/]+)", path):
                self._json(self.context.writer_jobs.get(match.group(1)))
            elif match := re.fullmatch(r"/api/semantic/jobs/([^/]+)", path):
                self._json(self.context.semantic_jobs.get(match.group(1)))
            elif match := re.fullmatch(r"/api/media/jobs/([^/]+)", path):
                self._json(self.context.media_jobs.get(match.group(1)))
            elif match := re.fullmatch(r"/api/media/([^/]+)/source", path):
                requested = self.context.media_pipeline.resolve_source(match.group(1))
                self._serve_media_file(requested, requested.name)
            elif match := re.fullmatch(r"/api/media/([^/]+)/clips/([^/]+)", path):
                requested = self.context.media_pipeline.resolve_clip(match.group(1), match.group(2))
                self._serve_media_file(requested, requested.name)
            elif match := re.fullmatch(r"/api/media/([^/]+)", path):
                self._json(self.context.media_pipeline.get_media(match.group(1)))
            elif match := re.fullmatch(r"/api/documents/([^/]+)/structure", path):
                self._json(structure_state(self.context, match.group(1)))
            elif match := re.fullmatch(r"/api/documents/([^/]+)/workflow", path):
                self._json(newsroom_state(self.context, match.group(1)))
            elif match := re.fullmatch(r"/api/documents/([^/]+)", path):
                self._json(self.context.repository.get_document(match.group(1)))
            elif match := re.fullmatch(r"/api/documents/([^/]+)/revisions", path):
                self._json({"items": self.context.repository.list_revisions(match.group(1))})
            elif match := re.fullmatch(r"/api/documents/([^/]+)/runs", path):
                self._json({"items": self.context.repository.list_runs(match.group(1))})
            elif match := re.fullmatch(r"/api/documents/([^/]+)/diff", path):
                document_id = match.group(1)
                from_id = query.get("from", [""])[0]
                to_id = query.get("to", [""])[0]
                old = self.context.repository.get_revision(document_id, from_id)
                new = self.context.repository.get_revision(document_id, to_id)
                diff = "".join(
                    difflib.unified_diff(
                        old["content"].splitlines(keepends=True),
                        new["content"].splitlines(keepends=True),
                        fromfile=from_id[:8],
                        tofile=to_id[:8],
                    )
                )
                self._json({"diff": diff, "from": old, "to": new})
            else:
                self._error(HTTPStatus.NOT_FOUND, "找不到路徑")
        except CatalogError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except WriterFailure as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.message)
        except MediaFailure as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.message)
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc).strip("'"))
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"伺服器錯誤：{exc}")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/media/upload":
            if (
                not self._allow_local_request()
                or not self._allow_media_mutation_request()
                or not self._allow_session_request(path)
            ):
                return
            try:
                filename = parse_qs(parsed.query).get("filename", [""])[0]
                clean_name = Path(str(filename or "").replace("\\", "/")).name
                if Path(clean_name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4a", ".mp3", ".wav"}:
                    self._discard_small_request_body()
                    raise MediaFailure("media_type_not_allowed", "影音格式只接受 mp4、mov、mkv、m4a、mp3、wav")
                raw_length = self.headers.get("Content-Length")
                content_length = int(raw_length) if raw_length is not None else None
                if content_length is not None and content_length > self.context.media_pipeline.config.max_upload_bytes:
                    self.close_connection = True
                    raise MediaFailure("media_too_large", "影音超過 3GB 上限")
                media = self.context.media_pipeline.create_upload(
                    clean_name,
                    self._upload_chunks(),
                    content_length=content_length,
                )
                self._json(media, HTTPStatus.CREATED)
            except MediaFailure as exc:
                status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if exc.code == "media_too_large" else HTTPStatus.BAD_REQUEST
                self._error(status, exc.message)
            except (ValueError, OSError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if (
            not self._allow_local_request()
            or not self._allow_mutation_request()
            or not self._allow_session_request(path)
        ):
            return
        try:
            data = self._read_json()
            if path == "/api/login":
                self._handle_login(data)
            elif path == "/api/documents":
                document = self.context.repository.create_document(
                    str(data.get("title") or "未命名文件"),
                    str(data.get("content") or ""),
                )
                self._json(document, HTTPStatus.CREATED)
            elif path == "/api/import":
                self._json(
                    import_document(str(data.get("filename") or ""), str(data.get("content_base64") or "")),
                    HTTPStatus.CREATED,
                )
            elif match := re.fullmatch(r"/api/media/([^/]+)/(transcribe|highlights|clips)", path):
                media_id, action = match.groups()
                job = self.context.media_jobs.start(media_id, action)
                self._json(job, HTTPStatus.ACCEPTED)
            elif match := re.fullmatch(r"/api/media/([^/]+)/document", path):
                started = time.perf_counter()
                media = self.context.media_pipeline.get_media(match.group(1))
                transcript = media.get("transcript")
                if not isinstance(transcript, dict) or not isinstance(transcript.get("segments"), list):
                    raise MediaFailure("transcript_missing", "逐字稿尚未完成，不能存成文件")
                content = "\n\n".join(
                    str(item.get("text") or "").strip()
                    for item in transcript["segments"]
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                )
                if not content:
                    raise MediaFailure("transcript_empty", "逐字稿沒有可存入文件的文字")
                title = str(data.get("title") or f"{media.get('original_filename') or '影音'}・逐字稿")[:180]
                document = self.context.repository.create_document(title, content)
                provenance = transcript.get("provenance") if isinstance(transcript.get("provenance"), dict) else {}
                output = {
                    "contract_version": "wanjuan_media_document_v1",
                    "media_id": media["id"],
                    "transcript_hash": json_hash(transcript),
                    "document_content_hash": json_hash(content),
                    "source_sha256": (media.get("provenance") or {}).get("source_sha256"),
                    "model": provenance.get("model"),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                }
                run = self.context.repository.record_run(
                    document["id"],
                    document["current_revision_id"],
                    "media_transcript_import",
                    output,
                    status="complete",
                    engine=str(provenance.get("model") or "human_import"),
                )
                self._json({"document": document, "run": run}, HTTPStatus.CREATED)
            elif path == "/api/recommend/styles":
                concept = str(data.get("concept") or "")
                limit = int(data.get("limit") or 5)
                self._json({"items": self.context.catalog.recommend_style_cards(concept, limit)})
            elif path == "/api/structure/detect":
                content = str(data.get("content") or "")
                if len(content.encode("utf-8")) > MAX_BODY_BYTES:
                    raise ValueError("稿件內容超過 2 MB")
                self._json(detect_structure(content))
            elif match := re.fullmatch(r"/api/documents/([^/]+)/structure", path):
                document_id = match.group(1)
                document = self.context.repository.get_document(document_id)
                if str(data.get("expected_revision_id") or "") != document["current_revision_id"]:
                    raise WorkflowConflictError("稿件版本已更新，請重新載入後再改判結構")
                overrides = data.get("overrides")
                if not isinstance(overrides, list):
                    raise ValueError("結構改判必須是清單")
                current = structure_state(self.context, document_id)
                updated = apply_structure_overrides(document["content"], current, overrides)
                run = self.context.repository.record_run(
                    document_id,
                    document["current_revision_id"],
                    "structure_override",
                    updated,
                    status="complete",
                    engine="human_structure",
                )
                result = structure_state(self.context, document_id)
                result["override_run_id"] = run["id"]
                self._json(result, HTTPStatus.CREATED)
            elif match := re.fullmatch(r"/api/documents/([^/]+)/preflight", path):
                document_id = match.group(1)
                document = self.context.repository.get_document(document_id)
                if str(data.get("expected_revision_id") or "") != document["current_revision_id"]:
                    raise WorkflowConflictError("稿件版本已更新，請重新載入後再預檢")
                source_notes = str(data.get("source_notes") or "")
                self._json(
                    review_content(
                        document["title"],
                        document["content"],
                        "chinatimes_newsroom",
                        source_notes,
                    )
                )
            elif match := re.fullmatch(r"/api/documents/([^/]+)/semantic-review", path):
                document_id = match.group(1)
                document = self.context.repository.get_document(document_id)
                if str(data.get("expected_revision_id") or "") != document["current_revision_id"]:
                    raise WorkflowConflictError("稿件版本已更新，請重新載入後再開始語義審")
                workflow = newsroom_state(self.context, document_id)
                if not workflow["writer"]["allowed_to_edit"]:
                    raise ValueError("寫手尚未完成；請先完成寫手出稿，或明確按下跳過寫手")
                structure = structure_state(self.context, document_id)
                expected_structure_hash = str(data.get("expected_structure_hash") or "")
                if expected_structure_hash and expected_structure_hash != structure["structure_hash"]:
                    raise WorkflowConflictError("段落結構已改判，請重新載入後再開始語義審")
                job = self.context.semantic_jobs.start(
                    document_id,
                    document["current_revision_id"],
                    document["title"],
                    document["content"],
                    {
                        key: structure[key]
                        for key in ("contract_version", "blocks", "summary")
                    },
                    writer_actual_engine=workflow["writer"]["actual_engine"],
                )
                self._json(job, HTTPStatus.ACCEPTED)
            elif match := re.fullmatch(r"/api/documents/([^/]+)/semantic-title", path):
                document_id = match.group(1)
                before = self.context.repository.get_document(document_id)
                if str(data.get("expected_revision_id") or "") != before["current_revision_id"]:
                    raise WorkflowConflictError("稿件版本已更新，請重新載入後再採用標題")
                run = self.context.repository.get_run(str(data.get("semantic_run_id") or ""))
                if (
                    run["document_id"] != document_id
                    or run["revision_id"] != before["current_revision_id"]
                    or run["action"] != "semantic_review"
                    or run["status"] != "complete"
                ):
                    raise ValueError("語義審紀錄與目前版本不一致")
                candidate_id = str(data.get("candidate_id") or "")
                candidates = (run.get("output") or {}).get("editor", {}).get("headline_candidates", [])
                candidate = next(
                    (item for item in candidates if item.get("id") == candidate_id),
                    None,
                )
                if not candidate:
                    raise ValueError("找不到這組下標候選")
                previous_structure = structure_state(self.context, document_id)
                source_hint = self.context.repository.get_source_hint(
                    document_id, before["current_revision_id"]
                )
                updated = self.context.repository.save_revision(
                    document_id,
                    str(candidate["main_title"]),
                    before["content"],
                    actor="human:semantic_title",
                    note="人工採用語義審下標候選",
                    force=True,
                )
                self.context.repository.save_source_hint(
                    document_id, updated["current_revision_id"], source_hint
                )
                self.context.repository.copy_source_decisions(
                    document_id,
                    before["current_revision_id"],
                    updated["current_revision_id"],
                )
                if any(item.get("source") == "human" for item in previous_structure["blocks"]):
                    detected = detect_structure(updated["content"])
                    carried = apply_structure_overrides(
                        updated["content"],
                        detected,
                        [
                            {"id": item["id"], "type": item["type"]}
                            for item in previous_structure["blocks"]
                            if item.get("source") == "human"
                        ],
                    )
                    self.context.repository.record_run(
                        document_id,
                        updated["current_revision_id"],
                        "structure_override",
                        carried,
                        status="complete",
                        engine="human_structure",
                    )
                self._json(
                    {
                        "document": updated,
                        "workflow": newsroom_state(self.context, document_id),
                        "structure": structure_state(self.context, document_id),
                    },
                    HTTPStatus.CREATED,
                )
            elif match := re.fullmatch(r"/api/documents/([^/]+)/sources", path):
                document_id = match.group(1)
                document = self.context.repository.get_document(document_id)
                if str(data.get("expected_revision_id") or "") != document["current_revision_id"]:
                    raise WorkflowConflictError("稿件版本已更新，請重新載入後再裁定來源")
                source_hint = self.context.repository.get_source_hint(
                    document_id,
                    document["current_revision_id"],
                )
                expected = {
                    item["clue_id"]: item
                    for item in extract_source_clues(document["content"], source_hint)
                }
                raw_decisions = data.get("decisions")
                if not isinstance(raw_decisions, list):
                    raise ValueError("來源裁定必須是清單")
                decisions = []
                for raw in raw_decisions:
                    if not isinstance(raw, dict):
                        raise ValueError("來源裁定格式錯誤")
                    clue_id = str(raw.get("clue_id") or "")
                    clue = expected.get(clue_id)
                    if not clue:
                        raise ValueError("來源線索已過期，請重新載入目前版本")
                    status = str(raw.get("status") or "pending")
                    if status not in SOURCE_STATUSES:
                        raise ValueError("來源狀態不支援")
                    if clue["clue_kind"] == "missing" and status not in {"pending", "gap"}:
                        raise ValueError("缺少來源的稿件不能標成已確認或存疑；請先補入真實來源")
                    note = str(raw.get("note") or "").strip()[:500]
                    decisions.append({**clue, "status": status, "note": note})
                if set(expected) != {item["clue_id"] for item in decisions}:
                    raise ValueError("請逐項裁定目前版本的所有來源線索")
                self.context.repository.save_source_decisions(
                    document_id,
                    document["current_revision_id"],
                    decisions,
                )
                self._json(newsroom_state(self.context, document_id))
            elif match := re.fullmatch(r"/api/documents/([^/]+)/review", path):
                document_id = match.group(1)
                document = self.context.repository.get_document(document_id)
                card_id = str(data.get("card_id") or "") or None
                template_id = str(data.get("template_id") or "") or None
                persona_id = str(data.get("persona_id") or "") or None
                chief_persona_id = str(data.get("chief_persona_id") or "") or None
                workflow_id = str(data.get("workflow_id") or "general")
                source_notes = str(data.get("source_notes") or "")
                if workflow_id not in {"general", "chinatimes_newsroom"}:
                    raise ValueError("不支援的審稿流程")
                if workflow_id == "chinatimes_newsroom":
                    workflow = newsroom_state(self.context, document_id)
                    if not workflow["writer"]["allowed_to_edit"]:
                        raise ValueError("寫手尚未完成；請先完成寫手出稿，或明確按下跳過寫手")
                    if not workflow["source_readiness"]["ready"]:
                        raise ValueError("請先逐項完成來源裁定，再開始新聞審稿")
                    source_notes = "\n".join(item["clue_text"] for item in workflow["sources"])
                validations = (
                    ("style_cards", card_id),
                    ("templates", template_id),
                    ("editors", persona_id),
                    ("editors", chief_persona_id),
                )
                for kind, asset_id in validations:
                    if not self.context.catalog.has_asset(kind, asset_id):
                        raise CatalogError(f"找不到所選資產：{asset_id}")
                output = review_content(document["title"], document["content"], workflow_id, source_notes)
                output["selection"] = {
                    "card_id": card_id,
                    "template_id": template_id,
                    "persona_id": persona_id,
                    "chief_persona_id": chief_persona_id,
                    "workflow_id": workflow_id,
                }
                run = self.context.repository.record_run(
                    document_id,
                    document["current_revision_id"],
                    "local_review",
                    output,
                    card_id,
                    template_id,
                    persona_id,
                )
                self._json(run, HTTPStatus.CREATED)
            elif match := re.fullmatch(r"/api/documents/([^/]+)/rewrite", path):
                document_id = match.group(1)
                before = self.context.repository.get_document(document_id)
                if "writer_card_id" in data:
                    if str(data.get("expected_revision_id") or "") != before["current_revision_id"]:
                        raise WorkflowConflictError("稿件版本已更新，請重新載入後再開始寫稿")
                    raw_target = data.get("target_length", 2000)
                    if isinstance(raw_target, bool) or not isinstance(raw_target, int):
                        raise ValueError("target_length 必須是整數")
                    job = self.context.writer_jobs.start(
                        document_id,
                        before["current_revision_id"],
                        before["title"],
                        before["content"],
                        str(data.get("writer_card_id") or ""),
                        raw_target,
                    )
                    self._json(job, HTTPStatus.ACCEPTED)
                    return
                if data.get("skip_writer") is True:
                    if str(data.get("expected_revision_id") or "") != before["current_revision_id"]:
                        raise WorkflowConflictError("稿件版本已更新，請重新載入後再跳過寫手")
                    existing = next(
                        (
                            item for item in self.context.repository.list_runs(document_id)
                            if item["action"] == "writer_skip"
                            and item["revision_id"] == before["current_revision_id"]
                            and item["status"] == "complete"
                        ),
                        None,
                    )
                    run = existing or self.context.repository.record_run(
                        document_id,
                        before["current_revision_id"],
                        "writer_skip",
                        {
                            "contract_version": "newsroom_writer_v1",
                            "workflow": {"id": "newsroom_writer", "status": "skipped"},
                            "notice": "使用者明確跳過寫手、直接審原稿",
                            "summary": {"total": 0},
                            "provenance": {"writer_actual_engine": None, "skipped": True},
                        },
                        status="complete",
                        engine="human_skip",
                    )
                    self._json(
                        {"run": run, "workflow": newsroom_state(self.context, document_id)},
                        HTTPStatus.CREATED,
                    )
                    return
                workflow = newsroom_state(self.context, document_id)
                if str(data.get("expected_revision_id") or "") != workflow["revision_id"]:
                    raise WorkflowConflictError("稿件版本已更新，請重新載入後再改寫")
                if str(data.get("expected_review_run_id") or "") != str(workflow["review_run_id"] or ""):
                    raise WorkflowConflictError("審稿結果已更新，請重新載入後再改寫")
                if not workflow["source_readiness"]["ready"]:
                    raise ValueError("來源線索尚未完成裁定，不能建立修訂版本")
                if not workflow["review_current"]:
                    raise ValueError("目前版本尚未完成新聞審稿")
                persona_id = str(data.get("persona_id") or "")
                if persona_id not in REWRITE_PERSONAS:
                    raise ValueError("請選擇可用的改寫人物卡")
                rewrite = rewrite_with_persona(before["content"], persona_id)
                source_hint = self.context.repository.get_source_hint(
                    document_id,
                    before["current_revision_id"],
                )
                updated = self.context.repository.save_revision(
                    document_id,
                    before["title"],
                    rewrite["content"],
                    actor=f"persona:{persona_id}",
                    note=f"{rewrite['persona_name']}建立修訂版本",
                    force=True,
                )
                self.context.repository.save_source_hint(
                    document_id,
                    updated["current_revision_id"],
                    source_hint,
                )
                self.context.repository.copy_source_decisions(
                    document_id,
                    before["current_revision_id"],
                    updated["current_revision_id"],
                )
                rewrite["source_revision_id"] = before["current_revision_id"]
                rewrite["generated_revision_id"] = updated["current_revision_id"]
                run = self.context.repository.record_run(
                    document_id,
                    updated["current_revision_id"],
                    "persona_rewrite",
                    rewrite,
                    persona_id=persona_id,
                    status="revision_created",
                    engine="local_rewrite",
                )
                self._json(
                    {
                        "document": updated,
                        "run": run,
                        "workflow": newsroom_state(self.context, document_id),
                    },
                    HTTPStatus.CREATED,
                )
            elif match := re.fullmatch(r"/api/documents/([^/]+)/approve", path):
                document_id = match.group(1)
                workflow = newsroom_state(self.context, document_id)
                if str(data.get("expected_revision_id") or "") != workflow["revision_id"]:
                    raise WorkflowConflictError("稿件版本已變更，請重新閱讀目前版本")
                if str(data.get("expected_review_run_id") or "") != str(workflow["review_run_id"] or ""):
                    raise WorkflowConflictError("審稿報告已更新，請重新閱讀目前報告")
                required_ack = ("ack_machine", "ack_sources", "ack_final")
                if not all(data.get(key) is True for key in required_ack):
                    raise ValueError("人工核准前須完成三項確認")
                if not workflow["source_readiness"]["ready"]:
                    raise ValueError("來源線索尚未處理完成")
                if not workflow["review_current"]:
                    raise ValueError("目前修訂版本尚未重新審稿")
                if not workflow["finalizable"]:
                    raise ValueError("目前版本尚未由人物卡建立修訂版本")
                approval = self.context.repository.record_approval(
                    document_id,
                    workflow["revision_id"],
                    workflow["review_run_id"],
                    workflow["sources"],
                    note=str(data.get("note") or "人工核准目前版本").strip()[:500],
                )
                self._json(
                    {"approval": approval, "workflow": newsroom_state(self.context, document_id)},
                    HTTPStatus.CREATED,
                )
            elif match := re.fullmatch(r"/api/documents/([^/]+)/video-plan", path):
                document_id = match.group(1)
                document = self.context.repository.get_document(document_id)
                workflow = newsroom_state(self.context, document_id)
                if str(data.get("expected_revision_id") or "") != workflow["revision_id"]:
                    raise WorkflowConflictError("稿件版本已更新，請重新載入後建立影音企劃")
                if str(data.get("expected_review_run_id") or "") != str(workflow["review_run_id"] or ""):
                    raise WorkflowConflictError("審稿結果已更新，請重新載入後建立影音企劃")
                if str(data.get("expected_approval_id") or "") != str((workflow["approval"] or {}).get("id") or ""):
                    raise WorkflowConflictError("人工核准狀態已更新，請重新載入後建立影音企劃")
                if not workflow["video_plan_eligible"]:
                    raise ValueError("影音企劃尚未解鎖：" + "；".join(workflow["blockers"]))
                source_notes = "\n".join(item["clue_text"] for item in workflow["sources"])
                review_run = self.context.repository.get_run(workflow["review_run_id"])
                output = build_video_plan(
                    document["title"],
                    document["content"],
                    source_notes,
                    review_run["output"],
                    review_run["id"],
                )
                output["status"] = {
                    "code": "approved_source_ready_draft",
                    "label": "核准後企劃草稿",
                    "dispatchable": False,
                    "reason": "稿件版本、來源裁定與人工核准已完成；實際派工仍需主管另行下令。",
                }
                output["notice"] = "目前稿件已完成版本綁定的人工核准；本功能只建立企劃草稿，不會自動派工。"
                output["risk_gate"] = [
                    item for item in output.get("risk_gate", [])
                    if item.get("id") != "human_approval"
                ]
                run = self.context.repository.record_run(
                    document_id,
                    document["current_revision_id"],
                    "video_plan_draft",
                    output,
                    status="approved_source_ready_draft",
                    engine="local_rules",
                )
                self._json(run, HTTPStatus.CREATED)
            elif match := re.fullmatch(r"/api/documents/([^/]+)/restore", path):
                self._json(
                    self.context.repository.restore_revision(match.group(1), str(data.get("revision_id") or "")),
                    HTTPStatus.CREATED,
                )
            else:
                self._error(HTTPStatus.NOT_FOUND, "找不到路徑")
        except WorkflowConflictError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except CatalogError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except WriterFailure as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.message)
        except MediaFailure as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.message)
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc).strip("'"))
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"伺服器錯誤：{exc}")

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if (
            not self._allow_local_request()
            or not self._allow_mutation_request()
            or not self._allow_session_request(path)
        ):
            return
        try:
            data = self._read_json()
            match = re.fullmatch(r"/api/documents/([^/]+)", path)
            if not match:
                self._error(HTTPStatus.NOT_FOUND, "找不到路徑")
                return
            document = self.context.repository.save_revision(
                match.group(1),
                str(data.get("title") or "未命名文件"),
                str(data.get("content") or ""),
                actor="human",
                note=str(data.get("note") or "手動儲存"),
            )
            self.context.repository.save_source_hint(
                match.group(1),
                document["current_revision_id"],
                str(data.get("source_hint") or ""),
            )
            self._json(document)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc).strip("'"))
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"伺服器錯誤：{exc}")


def build_server(host: str, port: int, context: AppContext) -> ThreadingHTTPServer:
    handler = type("ConfiguredStudioHandler", (StudioHandler,), {"context": context})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="萬卷文庫本機伺服器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--baigui-root",
        type=Path,
        default=Path(os.environ.get("BAIGUI_LIBRARY_ROOT", DEFAULT_BAIGUI_ROOT)),
    )
    parser.add_argument(
        "--chinatimes-root",
        type=Path,
        default=Path(os.environ.get("CHINATIMES_ROOT", DEFAULT_CHINATIMES_ROOT)),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("BAIGUI_STUDIO_DATA", PROJECT_ROOT / "data" / "studio.db")),
    )
    parser.add_argument(
        "--engine-config",
        type=Path,
        default=Path(os.environ.get("BAIGUI_ENGINE_CONFIG", PROJECT_ROOT / "data" / "engine.json")),
    )
    parser.add_argument(
        "--access-config",
        type=Path,
        default=Path(os.environ.get("BAIGUI_ACCESS_CONFIG", DEFAULT_ACCESS_CONFIG)),
    )
    args = parser.parse_args()
    access = load_access_config(args.access_config)
    external = args.host not in LOCAL_BIND_HOSTS
    if external and not access.allow_external:
        raise SystemExit(
            f"只允許綁定本機位址；要開放外部連線請先在 {args.access_config} 設定 allow_external=true。"
        )
    if external and not access.password:
        raise SystemExit(
            f"{args.access_config} 已允許外部連線，但沒有設定 password；請先設定密碼再開放。"
        )
    context = AppContext(
        args.baigui_root,
        args.database,
        args.chinatimes_root,
        args.engine_config,
        args.access_config,
    )
    server = build_server(args.host, args.port, context)
    print(f"萬卷文庫已啟動：http://{args.host}:{args.port}")
    if external:
        print("外部連線已開放：非本機來源須先於 /login 輸入密碼，session 有效 12 小時。")
        print("風險提醒：本服務走 HTTP 明文，密碼與稿件內容未加密；僅限區網或 Tailscale 等受信任網路。")
        for name in sorted(server_host_names(args.port)):
            if not name.startswith(("127.0.0.1", "[::1]")) and ":" in name.rsplit("]", 1)[-1]:
                print(f"  可用入口：http://{name}")
    print(f"百鬼卡庫：{args.baigui_root}")
    print(f"文件資料庫：{args.database}")
    print(f"語義引擎設定：{args.engine_config}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n萬卷文庫已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
