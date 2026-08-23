from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.media_pipeline import (
    MediaFailure,
    MediaJobManager,
    MediaPipeline,
    FasterWhisperTranscriber,
    load_media_config,
    normalize_highlight_output,
    plan_clips,
    probe_media,
)


class FakeTranscriber:
    def __init__(self, result: object | None = None, failure: Exception | None = None):
        self.result = result
        self.failure = failure
        self.calls: list[tuple[Path, str | None]] = []

    def transcribe(self, source_path: Path, *, language: str | None = None) -> object:
        self.calls.append((source_path, language))
        if self.failure:
            raise self.failure
        return self.result


class MediaPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "media_config.json").write_text(
            json.dumps(
                {
                    "whisper_model": "tiny-test",
                    "device": "auto",
                    "language": "zh",
                    "max_upload_bytes": 1024 * 1024,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.root / "data" / "engine.json").write_text(
            json.dumps(
                {
                    "name": "unused-test-engine",
                    "command": ["unused"],
                    "timeout_seconds": 10,
                    "output": "stdout",
                    "network_disclosure": "測試不呼叫",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fake_transcriber_writes_timestamped_transcript_and_provenance(self) -> None:
        fake = FakeTranscriber(
            {
                "segments": [
                    {"start": 0.0, "end": 1.25, "text": " 第一段 "},
                    {"start": 1.25, "end": 2.5, "text": "第二段"},
                ],
                "info": {"duration": 2.5, "language": "zh"},
                "model": "fake-whisper",
                "device": "cpu",
                "compute_type": "int8",
            }
        )
        pipeline = MediaPipeline(self.root, transcriber=fake)
        uploaded = pipeline.create_upload("訪談.MP4", [b"abc", b"def"], content_length=6)
        result = pipeline.transcribe(uploaded["id"])

        self.assertEqual([item["id"] for item in result["segments"]], ["segment-1", "segment-2"])
        self.assertEqual(result["text"], "第一段\n第二段")
        self.assertEqual(result["provenance"]["model"], "fake-whisper")
        self.assertEqual(result["provenance"]["actual_device"], "cpu")
        self.assertEqual(fake.calls[0][1], "zh")
        self.assertTrue(result["provenance"]["source_sha256"])
        self.assertTrue((self.root / "data" / "media" / uploaded["id"] / "transcript.json").is_file())

    def test_background_failure_is_honest_and_keeps_uploaded_source(self) -> None:
        fake = FakeTranscriber(failure=MediaFailure("whisper_missing_model", "測試模型不存在"))
        pipeline = MediaPipeline(self.root, transcriber=fake)
        uploaded = pipeline.create_upload("source.mp4", [b"not-a-real-video"])
        manager = MediaJobManager(pipeline)
        job = manager.start_transcription(uploaded["id"])
        deadline = time.time() + 3
        current = manager.get(job["id"])
        while current["status"] in {"queued", "running"} and time.time() < deadline:
            time.sleep(0.01)
            current = manager.get(job["id"])
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["error"]["code"], "whisper_missing_model")
        self.assertTrue(pipeline.resolve_source(uploaded["id"]).is_file())
        self.assertIsNone(pipeline.get_media(uploaded["id"])["transcript"])

    def test_auto_device_falls_back_from_cuda_to_cpu_int8(self) -> None:
        calls: list[tuple[str, str]] = []

        class FakeModel:
            def transcribe(self, _path: str, **_kwargs: object) -> tuple[list[object], object]:
                return [SimpleNamespace(start=0.0, end=1.0, text="退路成功")], SimpleNamespace(duration=1.0, language="zh", language_probability=1.0)

        def factory(_model: str, *, device: str, compute_type: str) -> FakeModel:
            calls.append((device, compute_type))
            if device == "cuda":
                raise RuntimeError("測試 CUDA 不可用")
            return FakeModel()

        adapter = FasterWhisperTranscriber(load_media_config(self.root / "data" / "media_config.json"), factory)
        result = adapter.transcribe(self.root / "source.mp4", language="zh")
        self.assertEqual(calls, [("cuda", "float16"), ("cpu", "int8")])
        self.assertEqual(result["device"], "cpu")
        self.assertEqual(result["compute_type"], "int8")
        self.assertEqual(result["fallbacks"][0]["device"], "cuda")

    def test_streaming_upload_stops_at_configured_cap_and_leaves_no_partial_media(self) -> None:
        (self.root / "data" / "media_config.json").write_text(
            json.dumps(
                {
                    "whisper_model": "tiny-test",
                    "device": "cpu",
                    "language": "auto",
                    "max_upload_bytes": 5,
                }
            ),
            encoding="utf-8",
        )
        pipeline = MediaPipeline(self.root, transcriber=FakeTranscriber([]))
        with self.assertRaises(MediaFailure) as caught:
            pipeline.create_upload("too-large.mp4", [b"123", b"456"])
        self.assertEqual(caught.exception.code, "media_too_large")
        self.assertEqual(list((self.root / "data" / "media").iterdir()), [])

    def test_highlight_normalization_clamps_scores_and_timecodes(self) -> None:
        transcript = [
            {"id": "segment-1", "start": 0.0, "end": 2.0, "text": "一"},
            {"id": "segment-2", "start": 2.0, "end": 4.0, "text": "二"},
        ]
        rows, warnings = normalize_highlight_output(
            {
                "segments": [
                    {
                        "segment_id": "segment-1",
                        "start": -99,
                        "end": 99,
                        "material_score": 12,
                        "humor_score": "4.5",
                        "controversy_score": -1,
                        "reason": "爆點",
                    },
                    {"segment_id": "segment-2", "scores": {"有料": 8}, "理由": "資訊完整"},
                ],
                "extra": True,
            },
            transcript,
            4.0,
        )
        self.assertEqual(rows[0]["start"], 0.0)
        self.assertEqual(rows[0]["end"], 2.0)
        self.assertEqual(rows[0]["scores"], {"material": 10.0, "humor": 4.5, "controversy": 0.0})
        self.assertEqual(rows[1]["scores"]["material"], 8.0)
        self.assertTrue(any("超出片長" in warning for warning in warnings))
        self.assertTrue(any("超出 0 到 10" in warning for warning in warnings))


class ClipPlannerTests(unittest.TestCase):
    @staticmethod
    def rows() -> list[dict[str, object]]:
        scores = [3, 8, 4, 10, 5, 7, 2, 9, 6, 8, 4, 7]
        return [
            {
                "segment_id": f"segment-{index + 1}",
                "start": float(index * 6),
                "end": float((index + 1) * 6),
                "text": f"段落 {index + 1}",
                "scores": {"material": score, "humor": index % 4, "controversy": index % 3},
                "reason": f"分數 {score}",
            }
            for index, score in enumerate(scores)
        ]

    def test_four_targets_use_only_segment_boundaries_and_distinct_edits(self) -> None:
        rows = self.rows()
        plans = plan_clips(rows, 72.0)
        self.assertEqual([item["target_seconds"] for item in plans], [10, 30, 60, 90])
        boundaries = {(item["start"], item["end"]) for item in rows}
        for plan in plans:
            self.assertGreater(plan["actual_seconds"], 0)
            self.assertTrue(plan["selection_reason"])
            self.assertTrue(plan["suggested_title"])
            for item in plan["timecodes"]:
                self.assertIn((item["start"], item["end"]), boundaries)
                self.assertLessEqual(item["end"], 72.0)
        self.assertEqual(len(plans[0]["timecodes"]), 1)
        self.assertLessEqual(len(plans[1]["timecodes"]), 2)
        self.assertIn("最高分爆點", plans[0]["timecodes"][0]["role"])
        self.assertTrue(any("開頭鉤子" in item["role"] for item in plans[2]["timecodes"]))
        self.assertTrue(any("收尾" in item["role"] for item in plans[2]["timecodes"]))
        self.assertNotEqual(plans[2]["timecodes"], plans[3]["timecodes"])

    def test_sixty_and_ninety_second_edits_stay_distinct_with_short_inputs(self) -> None:
        rows = self.rows()[:5]
        plans = {item["target_seconds"]: item for item in plan_clips(rows, 30.0)}
        self.assertNotEqual(plans[60]["timecodes"], plans[90]["timecodes"])

    def test_rejects_empty_or_out_of_range_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "沒有已評分段落"):
            plan_clips([], 10)
        with self.assertRaisesRegex(ValueError, "影片長度"):
            plan_clips(self.rows(), 0)


class FfmpegCutIntegrationTests(unittest.TestCase):
    def test_real_ffmpeg_cut_from_synthesized_five_second_clip(self) -> None:
        ffmpeg = shutil.which("ffmpeg") or str(Path(os.environ.get("FFMPEG_DIR", "")) / "ffmpeg.exe")
        if not Path(ffmpeg).is_file():
            self.skipTest("本機找不到 ffmpeg")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            (root / "data" / "media_config.json").write_text(
                json.dumps(
                    {
                        "whisper_model": "test",
                        "device": "cpu",
                        "language": "zh",
                        "max_upload_bytes": 20 * 1024 * 1024,
                    }
                ),
                encoding="utf-8",
            )
            (root / "data" / "engine.json").write_text(
                json.dumps(
                    {
                        "name": "unused",
                        "command": ["unused"],
                        "timeout_seconds": 10,
                        "output": "stdout",
                        "network_disclosure": "測試",
                    }
                ),
                encoding="utf-8",
            )
            source = root / "synth.mp4"
            generated = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=160x90:rate=10:duration=5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100:duration=5",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            pipeline = MediaPipeline(root, transcriber=FakeTranscriber([]), ffmpeg_path=ffmpeg)
            self.assertIsNotNone(pipeline.ffprobe_path, "本驗收必須使用 ffprobe，不接受只用 ffmpeg 訊息替代")
            with source.open("rb") as handle:
                uploaded = pipeline.create_upload("synth.mp4", iter(lambda: handle.read(4096), b""))
            media_dir = root / "data" / "media" / uploaded["id"]
            plans = [
                {
                    "target_seconds": target,
                    "actual_seconds": 3.0,
                    "timecodes": [{"segment_id": "segment-1", "start": 0.75, "end": 3.75}],
                    "selection_reason": "測試",
                    "suggested_title": "合成測試片",
                }
                for target in (10, 30, 60, 90)
            ]
            (media_dir / "highlights.json").write_text(
                json.dumps({"plans": plans}, ensure_ascii=False),
                encoding="utf-8",
            )
            result = pipeline.cut_clips(uploaded["id"])
            self.assertEqual(len(result["clips"]), 4)
            for clip in result["clips"]:
                clip_path = pipeline.resolve_clip(uploaded["id"], clip["filename"])
                self.assertTrue(clip_path.is_file())
                verification = probe_media(clip_path, pipeline.ffprobe_path, pipeline.ffmpeg_path)
                self.assertEqual(verification["method"], "ffprobe")
                self.assertEqual(verification["codecs"].get("video"), "h264")
                self.assertEqual(verification["codecs"].get("audio"), "aac")
                measured = verification["duration"]
                self.assertLessEqual(abs(measured - 3.0), 2.0)
                self.assertEqual(clip["sensory_label"], "吸不吸引人，感官未判，待人工裁定")
                self.assertEqual(clip["provenance"]["video_codec"], "libx264")
                self.assertEqual(clip["provenance"]["preset"], "veryfast")
                self.assertEqual(clip["provenance"]["audio_codec"], "aac")

    def test_ffmpeg_failure_keeps_highlights_and_does_not_publish_clips(self) -> None:
        ffmpeg = shutil.which("ffmpeg") or str(Path(os.environ.get("FFMPEG_DIR", "")) / "ffmpeg.exe")
        ffprobe = shutil.which("ffprobe") or str(Path(os.environ.get("FFMPEG_DIR", "")) / "ffprobe.exe")
        if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
            self.skipTest("本機缺少 ffmpeg 或 ffprobe")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            (root / "data" / "media_config.json").write_text(json.dumps({"whisper_model": "test", "device": "cpu", "language": "zh", "max_upload_bytes": 5_000_000}), encoding="utf-8")
            (root / "data" / "engine.json").write_text(json.dumps({"name": "unused", "command": ["unused"], "timeout_seconds": 10, "output": "stdout", "network_disclosure": "測試"}), encoding="utf-8")
            source = root / "bad.mp4"
            generated = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=size=64x64:duration=2", "-c:v", "libx264", str(source)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            pipeline = MediaPipeline(root, transcriber=FakeTranscriber([]), ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
            uploaded = pipeline.create_upload("bad.mp4", [source.read_bytes()])
            pipeline.ffmpeg_path = Path(ffprobe)  # 刻意把切割命令送進錯誤可執行檔，模擬 ffmpeg 錯參數。
            media_dir = root / "data" / "media" / uploaded["id"]
            highlights_path = media_dir / "highlights.json"
            highlights_path.write_text(json.dumps({"plans": [{"target_seconds": 10, "timecodes": [{"start": 0, "end": 1}]}]}), encoding="utf-8")
            before = highlights_path.read_bytes()
            with self.assertRaises(MediaFailure):
                pipeline.cut_clips(uploaded["id"])
            self.assertEqual(highlights_path.read_bytes(), before)
            self.assertFalse((media_dir / "clips.json").exists())


if __name__ == "__main__":
    unittest.main()
