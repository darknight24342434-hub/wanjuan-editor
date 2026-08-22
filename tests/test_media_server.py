from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from src.media_pipeline import MediaPipeline
from src.server import MAX_BODY_BYTES, AppContext, build_server


TEST_PASSWORD = "media-test-0857"
EXTERNAL_HOST = "media.example"


class FakeTranscriber:
    def transcribe(self, source_path: Path, *, language: str | None = None) -> list[dict]:
        return [{"start": 0.0, "end": 1.0, "text": "測試逐字稿"}]


class FakeMediaJobs:
    """端點驗證只需可輪詢的工作；不啟動 whisper、語義引擎或 ffmpeg。"""

    def __init__(self, pipeline: MediaPipeline):
        self.pipeline = pipeline
        self.jobs: dict[str, dict] = {}

    def start(self, media_id: str, action: str) -> dict:
        self.pipeline.get_media(media_id)
        job_id = f"job-{len(self.jobs) + 1}"
        job = {
            "id": job_id,
            "media_id": media_id,
            "action": action,
            "status": "queued",
            "pass": "queued",
            "error": None,
        }
        self.jobs[job_id] = job
        return dict(job)

    def get(self, job_id: str) -> dict:
        try:
            return dict(self.jobs[job_id])
        except KeyError as exc:
            raise KeyError("找不到影音工作") from exc


class MediaServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        data_root = self.root / "data"
        data_root.mkdir(parents=True)
        (data_root / "media_config.json").write_text(
            json.dumps(
                {
                    "whisper_model": "tiny",
                    "device": "cpu",
                    "language": "zh",
                    "max_upload_bytes": 4 * 1024 * 1024,
                }
            ),
            encoding="utf-8",
        )

        catalog_root = self.root / "catalog"
        catalog_root.mkdir()
        (catalog_root / "INDEX.yaml").write_text(
            "updated: '2026-08-13'\nactive_assets:\n"
            "  style_cards: []\n  templates: []\n  editors: []\n",
            encoding="utf-8",
        )

        access_path = self.root / "access.json"
        access_path.write_text(
            json.dumps(
                {
                    "password": TEST_PASSWORD,
                    "allow_external": True,
                    "extra_hosts": [EXTERNAL_HOST],
                }
            ),
            encoding="utf-8",
        )

        pipeline = MediaPipeline(
            self.root,
            transcriber=FakeTranscriber(),
            ffmpeg_path=self.root / "missing-ffmpeg",
            ffprobe_path=self.root / "missing-ffprobe",
        )
        self.context = AppContext(
            catalog_root,
            data_root / "studio.db",
            self.root,
            self.root / "missing-engine.json",
            access_path,
            media_pipeline=pipeline,
        )
        self.context.media_jobs = FakeMediaJobs(pipeline)
        self.server = build_server("127.0.0.1", 0, self.context)
        self.port = int(self.server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(
        self,
        path: str,
        method: str = "GET",
        body: bytes | None = None,
        *,
        content_type: str = "application/json",
        cookie: str = "",
        origin: str | None = None,
        host: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Content-Type": content_type}
        if origin is not None:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        if host:
            headers["Host"] = host
        if extra_headers:
            headers.update(extra_headers)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def request_json(
        self,
        path: str,
        method: str = "GET",
        payload: dict | None = None,
        **kwargs,
    ) -> tuple[int, dict[str, str], dict]:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method != "GET" else None
        status, headers, raw = self.request(
            path,
            method,
            body,
            content_type="application/json",
            **kwargs,
        )
        return status, headers, json.loads(raw.decode("utf-8")) if raw else {}

    def upload(
        self,
        filename: str,
        body: bytes,
        **kwargs,
    ) -> tuple[int, dict[str, str], dict]:
        path = "/api/media/upload?" + urllib.parse.urlencode({"filename": filename})
        status, headers, raw = self.request(
            path,
            "POST",
            body,
            content_type="video/mp4",
            **kwargs,
        )
        return status, headers, json.loads(raw.decode("utf-8")) if raw else {}

    def login(self, **kwargs) -> str:
        status, headers, _ = self.request_json(
            "/api/login",
            "POST",
            {"password": TEST_PASSWORD},
            **kwargs,
        )
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def create_media_with_clip(self) -> tuple[dict, bytes, bytes]:
        source_bytes = b"source-media-bytes"
        media = self.context.media_pipeline.create_upload(
            "gate.mp4",
            [source_bytes],
            content_length=len(source_bytes),
        )
        clip_bytes = b"0123456789abcdefghijklmnopqrstuvwxyz"
        clip_dir = self.context.media_pipeline.media_root / media["id"] / "clips"
        clip_dir.mkdir()
        (clip_dir / "clip_10s_1.mp4").write_bytes(clip_bytes)
        return media, source_bytes, clip_bytes

    def test_raw_upload_larger_than_json_limit_streams_to_disk(self) -> None:
        body = b"m" * (MAX_BODY_BYTES + 8193)
        status, _, media = self.upload("large.mp4", body, origin=self.base)

        self.assertEqual(status, 201)
        self.assertEqual(media["size_bytes"], len(body))
        self.assertTrue(media["provenance"]["streamed_to_disk"])
        source = self.context.media_pipeline.resolve_source(media["id"])
        self.assertEqual(source.stat().st_size, len(body))
        self.assertEqual(source.read_bytes(), body)

    def test_http_chunked_upload_is_decoded_and_streamed_to_disk(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            path = "/api/media/upload?" + urllib.parse.urlencode({"filename": "chunked.mp4"})
            connection.request(
                "POST",
                path,
                body=iter((b"chunk-one-", b"chunk-two")),
                headers={"Content-Type": "video/mp4", "Origin": self.base},
                encode_chunked=True,
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(response.status, 201)
        source = self.context.media_pipeline.resolve_source(payload["id"])
        self.assertEqual(source.read_bytes(), b"chunk-one-chunk-two")

    def test_upload_rejects_unsupported_extension(self) -> None:
        status, _, payload = self.upload("payload.exe", b"reject-before-body", origin=self.base)
        self.assertEqual(status, 400)
        self.assertIn("只接受", payload["error"])
        self.assertFalse(any(self.context.media_pipeline.media_root.iterdir()))

    def test_transcript_can_be_saved_as_normal_document_with_provenance(self) -> None:
        media = self.context.media_pipeline.create_upload("talk.mp4", [b"source"])
        media_dir = self.context.media_pipeline.media_root / media["id"]
        transcript = {
            "segments": [
                {"id": "segment-1", "start": 0, "end": 1, "text": "第一段"},
                {"id": "segment-2", "start": 1, "end": 2, "text": "第二段"},
            ],
            "provenance": {"model": "fake-whisper", "input_hash": "in", "output_hash": "out"},
        }
        (media_dir / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        status, _, payload = self.request_json(
            f"/api/media/{media['id']}/document",
            "POST",
            {"title": "訪談逐字稿"},
            origin=self.base,
        )
        self.assertEqual(status, 201)
        document = self.context.repository.get_document(payload["document"]["id"])
        self.assertEqual(document["content"], "第一段\n\n第二段")
        self.assertEqual(payload["run"]["action"], "media_transcript_import")
        self.assertEqual(payload["run"]["output"]["model"], "fake-whisper")

    def test_media_upload_enforces_origin_and_cross_site_csrf(self) -> None:
        status, _, payload = self.upload(
            "bad-origin.mp4",
            b"media",
            origin="https://evil.example",
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", payload["error"])

        status, _, payload = self.upload(
            "cross-site.mp4",
            b"media",
            origin=self.base,
            extra_headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertIn("跨站", payload["error"])

    def test_remote_media_endpoints_require_session_then_allow_read_and_range(self) -> None:
        media, source_bytes, clip_bytes = self.create_media_with_clip()
        media_id = media["id"]
        clip_path = f"/api/media/{media_id}/clips/clip_10s_1.mp4"

        with mock.patch("src.server.client_is_local", return_value=False):
            self.assertEqual(self.upload("remote.mp4", b"remote", origin=self.base)[0], 401)
            self.assertEqual(
                self.request_json(
                    f"/api/media/{media_id}/transcribe",
                    "POST",
                    {},
                    origin=self.base,
                )[0],
                401,
            )
            self.assertEqual(self.request_json(f"/api/media/{media_id}")[0], 401)
            self.assertEqual(self.request(f"/api/media/{media_id}/source")[0], 401)
            self.assertEqual(self.request(clip_path)[0], 401)

            cookie = self.login(origin=self.base)
            upload_status, _, uploaded = self.upload(
                "remote-ok.mp4",
                b"remote-ok",
                origin=self.base,
                cookie=cookie,
            )
            self.assertEqual(upload_status, 201)
            self.assertEqual(uploaded["size_bytes"], len(b"remote-ok"))

            status, _, loaded = self.request_json(f"/api/media/{media_id}", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(loaded["id"], media_id)

            status, _, raw_source = self.request(f"/api/media/{media_id}/source", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(raw_source, source_bytes)

            status, _, job = self.request_json(
                f"/api/media/{media_id}/transcribe",
                "POST",
                {},
                origin=self.base,
                cookie=cookie,
            )
            self.assertEqual(status, 202)
            self.assertEqual(
                self.request_json(f"/api/media/jobs/{job['id']}", cookie=cookie)[0],
                200,
            )

            status, headers, ranged = self.request(
                clip_path,
                cookie=cookie,
                extra_headers={"Range": "bytes=5-12"},
            )
            self.assertEqual(status, 206)
            self.assertEqual(headers["Accept-Ranges"], "bytes")
            self.assertEqual(headers["Content-Range"], f"bytes 5-12/{len(clip_bytes)}")
            self.assertEqual(headers["Content-Length"], "8")
            self.assertEqual(ranged, clip_bytes[5:13])

    def test_non_local_host_requires_session_even_for_loopback_peer(self) -> None:
        media, _, _ = self.create_media_with_clip()
        media_path = f"/api/media/{media['id']}"

        status, _, _ = self.request_json(
            media_path,
            host=EXTERNAL_HOST,
            origin=f"https://{EXTERNAL_HOST}",
        )
        self.assertEqual(status, 401)

        cookie = self.login(host=EXTERNAL_HOST, origin=f"https://{EXTERNAL_HOST}")
        self.assertEqual(
            self.request_json(
                media_path,
                cookie=cookie,
                host=EXTERNAL_HOST,
                origin=f"https://{EXTERNAL_HOST}",
            )[0],
            200,
        )
        self.assertEqual(
            self.upload(
                "host-ok.mp4",
                b"host-ok",
                cookie=cookie,
                host=EXTERNAL_HOST,
                origin=f"https://{EXTERNAL_HOST}",
            )[0],
            201,
        )

        self.assertEqual(
            self.request_json(
                media_path,
                cookie=cookie,
                host="evil.example",
                origin="https://evil.example",
            )[0],
            403,
        )


if __name__ == "__main__":
    unittest.main()
