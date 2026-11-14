"""Offline CPU-only source tests. No HTTP, cookies, GPU, QQ, or paid operations.

Run from the repository root:
  python -B -m unittest discover -s pipeline/tests -p test_source.py -v
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from hq_cover import source


def make_track(track_id=123, title="Test Song", artist="Test Artist", album="Studio Album", duration=1000):
    return {"id": track_id, "title": title, "artists": [{"id": 9, "name": artist}],
            "album": {"id": 7, "name": album}, "duration_ms": duration}


class Response:
    def __init__(self, payload=None, status=200, headers=None, chunks=()):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.headers = {"Cookie": "SHOULD_BE_CLEARED"}
        self.cookies = mock.Mock()
        self.auth = ("unexpected", "auth")
        self.trust_env = True
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-test-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Global tripwires in every acquisition/validation test: real network is forbidden.
        self.auth_guard = mock.patch.object(source, "_authorized_session", side_effect=AssertionError("network forbidden"))
        self.cdn_guard = mock.patch.object(source.requests, "Session", side_effect=AssertionError("network forbidden"))
        self.auth_guard.start()
        self.cdn_guard.start()
        self.addCleanup(self.auth_guard.stop)
        self.addCleanup(self.cdn_guard.stop)
        self.cache_guard = mock.patch.object(source, "CACHE_ROOTS", ())
        self.cache_guard.start()
        self.addCleanup(self.cache_guard.stop)

    def make_flac(self, bits=16, channels=2, filename="fixture.flac"):
        path = self.root / filename
        sr = 8000
        rng = np.random.default_rng(42)
        if bits == 16:
            pcm = rng.integers(-32768, 32768, size=(sr, channels), dtype=np.int16)
            pcm[0, :] = -32768
            pcm[1, :] = 32767
        else:
            pcm = rng.integers(-8388608, 8388608, size=(sr, channels), dtype=np.int32) << 8
            pcm[0, :] = -2147483648
            pcm[1, :] = 2147483392
        sf.write(str(path), pcm, sr, format="FLAC", subtype=f"PCM_{bits}")
        data = path.read_bytes()
        quality = {"id": 123, "code": 200, "level": "lossless", "type": "flac",
                   "size": len(data), "md5": hashlib.md5(data).hexdigest(), "sr": sr}
        return path, make_track(), quality

    def make_cache(self, bits=16, track_id=123):
        path, track, quality = self.make_flac(bits)
        track["id"] = quality["id"] = track_id
        verified = source.validate_lossless(path, track, quality)
        record = {"status": "verified_official_lossless", "source": "NetEase Cloud Music official API",
                  "download_path": str(path.resolve()), "track": track, "quality_response": quality,
                  "trial_restricted": False, "transcoded": False, **verified}
        manifest = self.root / "source_metadata.json"
        manifest.write_text(json.dumps(record), encoding="utf-8")
        return manifest, record, path


class IdentityTests(unittest.TestCase):
    def test_exact_title_artist_and_common_separators(self):
        track = make_track(title="Love Story", artist="Taylor Swift")
        for query in ("Love Story Taylor Swift", "Love Story - Taylor Swift", "Taylor Swift / Love Story", "《Love Story》—Taylor Swift"):
            self.assertEqual(source.select_candidate(query, [track])["id"], 123)

    def test_chinese_title_artist(self):
        track = make_track(100001, "示例歌曲", "示例歌手", "示例专辑")
        self.assertEqual(source.select_candidate("示例歌曲 示例歌手", [track])["id"], 100001)

    def test_title_only_requires_review_even_single_candidate(self):
        with self.assertRaises(source.NeedsReview):
            source.select_candidate("Test Song", [make_track()])

    def test_explicit_id_accepts_title_only_but_never_wrong_query(self):
        self.assertEqual(source.select_candidate("Test Song", [make_track()], 123)["id"], 123)
        for query in ("Other Song", "Test Song Different Artist"):
            with self.assertRaises(source.NeedsReview):
                source.select_candidate(query, [make_track()], 123)

    def test_same_title_wrong_performer_never_matches(self):
        with self.assertRaises(source.NeedsReview):
            source.select_candidate("Test Song Test Artist", [make_track(artist="Someone Else")])

    def test_multiple_releases_need_human_review(self):
        with self.assertRaises(source.NeedsReview) as caught:
            source.select_candidate("Test Song Test Artist", [make_track(), make_track(track_id=456)])
        self.assertEqual(len(caught.exception.candidates), 2)

    def test_live_cover_karaoke_not_silently_selected(self):
        for title in ("Test Song (Live)", "Test Song 翻唱版", "Test Song (Karaoke)", "Test Song Remix"):
            with self.assertRaises(source.NeedsReview):
                source.select_candidate("Test Song Test Artist", [make_track(title=title)])
        with self.assertRaises(source.NeedsReview):
            source.select_candidate("Test Song Test Artist", [make_track(album="Concert Live 2020")])

    def test_fuzzy_partial_title_artist_rejected(self):
        with self.assertRaises(source.NeedsReview):
            source.select_candidate("Test Test Artist", [make_track()])
        with self.assertRaises(source.NeedsReview):
            source.select_candidate("Test Song Test", [make_track()])

    def test_all_collaborators_required(self):
        track = make_track()
        track["artists"].append({"id": 10, "name": "Guest"})
        with self.assertRaises(source.NeedsReview):
            source.select_candidate("Test Song Test Artist", [track])
        self.assertEqual(source.select_candidate("Test Song Test Artist / Guest", [track])["id"], 123)

    def test_duplicate_id_not_an_ambiguity(self):
        self.assertEqual(source.select_candidate("Test Song Test Artist", [make_track(), make_track()])["id"], 123)


class ValidationTests(FixtureCase):
    def test_pcm16_complete_decode(self):
        path, track, quality = self.make_flac(16)
        result = source.validate_lossless(path, track, quality)
        self.assertEqual(result["codec"]["bits_per_sample"], 16)
        self.assertTrue(result["verification"]["flac_streaminfo_pcm_md5_match"])
        self.assertEqual(result["verification"]["decoded_frames"], 8000)

    def test_pcm24_signed_samples_and_stereo_interleave(self):
        path, track, quality = self.make_flac(24)
        result = source.validate_lossless(path, track, quality)
        self.assertEqual(result["codec"]["bits_per_sample"], 24)
        self.assertTrue(result["verification"]["flac_streaminfo_pcm_md5_match"])

    def test_pcm24_mono(self):
        path, track, quality = self.make_flac(24, channels=1)
        self.assertEqual(source.validate_lossless(path, track, quality)["codec"]["channels"], 1)

    def test_api_encode_type_mp3_does_not_override_bitstream(self):
        path, track, quality = self.make_flac()
        quality["encodeType"] = "mp3"
        checked = source._quality(quality, track["id"])
        self.assertNotIn("encodeType", checked)
        self.assertEqual(source.validate_lossless(path, track, checked)["codec"]["format"], "FLAC")

    def test_lossy_tier_and_trial_rejected(self):
        _, track, quality = self.make_flac()
        for changes in ({"level": "standard"}, {"type": "mp3"}, {"freeTrialInfo": {"start": 0, "end": 30}}, {"md5": None}):
            with self.assertRaises(source.SourceError):
                source._quality({**quality, **changes}, track["id"])

    def test_nullable_trial_privilege_is_not_a_failure(self):
        _, track, quality = self.make_flac()
        checked = source._quality({**quality, "freeTrialPrivilege": None, "freeTrialInfo": None}, track["id"])
        self.assertEqual(checked["type"], "flac")

    def test_empty_trial_descriptor_is_still_rejected(self):
        _, track, quality = self.make_flac()
        with self.assertRaises(source.SourceError):
            source._quality({**quality, "freeTrialInfo": {}}, track["id"])

    def test_duration_sample_rate_and_depth_mismatch(self):
        path, track, quality = self.make_flac()
        for t, q in (({**track, "duration_ms": 5000}, quality), (track, {**quality, "sr": 44100}), (track, {**quality, "bitDepth": 24})):
            with self.assertRaises(source.SourceError):
                source.validate_lossless(path, t, q)

    def test_wrong_sha_rejected(self):
        path, track, quality = self.make_flac()
        with self.assertRaises(source.SourceError) as caught:
            source.validate_lossless(path, track, quality, "0" * 64)
        self.assertEqual(caught.exception.code, "cache_sha256")

    def test_official_file_md5_mismatch(self):
        path, track, quality = self.make_flac()
        with self.assertRaises(source.SourceError) as caught:
            source.validate_lossless(path, track, {**quality, "md5": "0" * 32})
        self.assertEqual(caught.exception.code, "file_md5")

    def test_fake_flac_extension_rejected_even_when_hash_matches(self):
        path = self.root / "pretend.flac"
        path.write_bytes(b"ID3" + b"x" * 100)
        quality = {"size": path.stat().st_size, "md5": hashlib.md5(path.read_bytes()).hexdigest()}
        with self.assertRaises(source.SourceError) as caught:
            source.validate_lossless(path, make_track(), quality)
        self.assertEqual(caught.exception.code, "flac_header")

    def test_streaminfo_pcm_md5_tamper_rejected(self):
        path, track, quality = self.make_flac()
        data = bytearray(path.read_bytes())
        data[26] ^= 1  # first byte of the STREAMINFO PCM MD5
        path.write_bytes(data)
        quality["md5"] = hashlib.md5(data).hexdigest()
        with self.assertRaises(source.SourceError) as caught:
            source.validate_lossless(path, track, quality)
        self.assertEqual(caught.exception.code, "pcm_md5")

    def test_incomplete_bitstream_rejected_even_if_file_hash_updated(self):
        path, track, quality = self.make_flac()
        data = path.read_bytes()[:-200]
        path.write_bytes(data)
        quality.update(size=len(data), md5=hashlib.md5(data).hexdigest())
        with self.assertRaises(source.SourceError):
            source.validate_lossless(path, track, quality)


class LyricsTests(unittest.TestCase):
    def test_credits_multiple_timestamps_and_offsets(self):
        parsed = source.parse_lrc("[offset:-100]\n[00:00.00]作词：某人\n[00:01.20][00:03.456]真的歌词\n[00:02.00]Composer: Somebody\n[00:01.20]真的歌词", 123)
        self.assertEqual(parsed["entries"], [{"time": 1.1, "text": "真的歌词"}, {"time": 3.356, "text": "真的歌词"}])
        self.assertEqual(parsed["timing_role"], "auxiliary_only_not_acoustic_boundaries")

    def test_credits_with_space_delimiters_removed(self):
        parsed = source.parse_lrc("[00:00.0]作词 某人\n[00:00.1]作曲 某人\n[00:00.2]Lyricist Someone\n[00:00.3]这是真歌词", 123)
        self.assertEqual(parsed["entries"], [{"time": 0.3, "text": "这是真歌词"}])

    def test_metadata_not_lyrics_and_empty_lyrics_not_instrumental(self):
        parsed = source.parse_lrc("[ti:Song]\n[ar:Artist]\n[00:00.0]\n[00:01.0]", 123)
        self.assertEqual(parsed["entries"], [])
        self.assertFalse(parsed["instrumental"])
        self.assertFalse(parsed["available"])

    def test_explicit_instrumental_marker(self):
        for lrc in ("[00:00.00]纯音乐，请欣赏", "[00:00.00]Instrumental", "纯音乐，请欣赏"):
            parsed = source.parse_lrc(lrc, 123)
            self.assertTrue(parsed["instrumental"])
            self.assertEqual(parsed["entries"], [])
        self.assertTrue(source.parse_lrc("", 123, instrumental=True)["instrumental"])

    def test_no_lyric_vs_uncollected(self):
        for payload, expected in (({"code": 200, "nolyric": True}, True), ({"code": 200, "uncollected": True}, False)):
            result = source._lyrics(Session([Response(payload)]), 123)
            self.assertEqual(result["instrumental"], expected)

    def test_lyric_network_failure_is_optional_and_sanitized(self):
        result = source._lyrics(Session([OSError("https://secret.invalid/?cookie=SECRET")]), 123)
        self.assertFalse(result["available"])
        self.assertNotIn("SECRET", json.dumps(result))


class NetworkBoundaryTests(FixtureCase):
    def test_cdn_allowlist_rejects_credentials_and_lookalikes(self):
        for url in ("https://m1.music.126.net/token/file", "http://m1.music.126.net/a"):
            self.assertTrue(source._valid_cdn(url))
        for url in ("https://music.126.net.evil.test/a", "https://evil.test/a", "https://user:secret@m1.music.126.net/a", "file:///a", "http://127.0.0.1/a", "https://m1.music.126.net:999/a"):
            self.assertFalse(source._valid_cdn(url))

    def test_api_no_redirect_cookie_leak_and_response_closed(self):
        response = Response({"code": 200})
        session = Session([response])
        source._official_get(session, "/api/song/detail", {"id": 123})
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertEqual(session.calls[0][0], "https://music.163.com/api/song/detail")
        self.assertTrue(response.closed)
        with self.assertRaises(source.SourceError):
            source._official_get(session, "https://evil.test/api", {})

    def test_api_exception_does_not_disclose_signed_url_or_cookie(self):
        with self.assertRaises(source.SourceError) as caught:
            source._official_get(Session([OSError("https://private/?signed=TOKEN Cookie=COOKIE")]), "/api/song/detail", {})
        self.assertNotIn("TOKEN", str(caught.exception))
        self.assertNotIn("COOKIE", str(caught.exception))

    def test_cdn_separate_session_identity_encoding_and_misleading_mime(self):
        response = Response(headers={"Content-Type": "audio/mpeg", "Content-Length": "4"}, chunks=[b"fLaC"])
        session = Session([response])
        destination = self.root / "source.part"
        with mock.patch.object(source.requests, "Session", return_value=session):
            source._download("https://m1.music.126.net/SECRET", destination, {"size": 4})
        self.assertEqual(destination.read_bytes(), b"fLaC")
        self.assertNotIn("Cookie", session.headers)
        self.assertFalse(session.trust_env)
        self.assertIsNone(session.auth)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertEqual(session.calls[0][1]["timeout"], (20, 45))
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)

    def test_redirect_outside_cdn_rejected_before_second_request(self):
        response = Response(status=302, headers={"Location": "https://evil.test/?secret=TOKEN"})
        session = Session([response])
        with mock.patch.object(source.requests, "Session", return_value=session):
            with self.assertRaises(source.SourceError) as caught:
                source._download("https://m1.music.126.net/token", self.root / "source.part", {"size": 4})
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("TOKEN", str(caught.exception))

    def test_https_downgrade_rejected(self):
        session = Session([Response(status=302, headers={"Location": "http://m1.music.126.net/a"})])
        with mock.patch.object(source.requests, "Session", return_value=session):
            with self.assertRaises(source.SourceError):
                source._download("https://m1.music.126.net/a", self.root / "source.part", {"size": 4})

    def test_partial_download_retained_and_error_redacted(self):
        session = Session([Response(chunks=[b"fLaC", OSError("TOKEN cookie=SECRET")])])
        destination = self.root / "source.part"
        with mock.patch.object(source.requests, "Session", return_value=session):
            with self.assertRaises(source.SourceError) as caught:
                source._download("https://m1.music.126.net/TOKEN", destination, {"size": 8})
        self.assertEqual(destination.read_bytes(), b"fLaC")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_stream_read1_returns_small_arrivals_for_deadline_checks(self):
        response = Response()
        response.raw = mock.Mock()
        response.raw.read1.side_effect = [b"f", b"L", b"aC", b""]
        self.assertEqual(list(source._stream_blocks(response)), [b"f", b"L", b"aC"])
        response.raw.read1.assert_called_with(256 * 1024, decode_content=False)

    def test_legacy_streaming_uses_one_byte_deadline_granularity(self):
        response = Response()
        response.iter_content = mock.Mock(return_value=iter([b"f", b"L"]))
        self.assertEqual(list(source._stream_blocks(response)), [b"f", b"L"])
        response.iter_content.assert_called_once_with(chunk_size=1)

    def test_budget_is_generous_and_enforced(self):
        self.assertGreaterEqual(source.DOWNLOAD_BUDGET_SECONDS, 900)
        session = Session([Response(chunks=[b"fLaC"])])
        with mock.patch.object(source.requests, "Session", return_value=session), mock.patch.object(source.time, "monotonic", side_effect=[0, 1, source.DOWNLOAD_BUDGET_SECONDS + 1]):
            with self.assertRaises(source.SourceError) as caught:
                source._download("https://m1.music.126.net/a", self.root / "source.part", {"size": 4})
        self.assertEqual(caught.exception.code, "download_budget")


class AcquisitionTests(FixtureCase):
    def test_cache_reuse_json_contract_without_network(self):
        _, record, original = self.make_cache(24)
        with mock.patch.object(source, "CACHE_ROOTS", (self.root,)):
            result = source.acquire_lossless("Test Song", self.root / "attempt", 123)
        self.assertTrue(result["cache_reused"])
        self.assertEqual(result["source_sha256"], record["download"]["sha256"])
        self.assertEqual(Path(result["source_path"]).read_bytes(), original.read_bytes())
        self.assertEqual(result["lyrics"]["entries"], [])
        self.assertFalse(result["lyrics"]["instrumental"])
        self.assertEqual(result["track"], record["track"])
        self.assertEqual(json.loads(Path(result["source_metadata_path"]).read_text(encoding="utf-8")), result)
        json.dumps(result, allow_nan=False)
        self.assertFalse((self.root / "attempt" / "source.flac.part").exists())
        self.assertFalse((self.root / "attempt" / "source_failure.json").exists())

    def test_existing_workdir_is_never_overwritten(self):
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "untouched"
        marker.write_text("original", encoding="utf-8")
        with self.assertRaises(source.SourceError) as caught:
            source.acquire_lossless("Test Song Test Artist", existing)
        self.assertEqual(caught.exception.code, "workdir_exists")
        self.assertEqual(list(existing.iterdir()), [marker])
        self.assertEqual(marker.read_text(encoding="utf-8"), "original")

    def test_corrupt_matching_cache_fails_without_redownload(self):
        _, _, original = self.make_cache()
        data = bytearray(original.read_bytes())
        data[-5] ^= 1
        original.write_bytes(data)
        with mock.patch.object(source, "CACHE_ROOTS", (self.root,)):
            with self.assertRaises(source.SourceError):
                source.acquire_lossless("Test Song", self.root / "attempt", 123)
        failure = json.loads((self.root / "attempt" / "source_failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["status"], "failed")
        self.assertFalse((self.root / "attempt" / "source_metadata.json").exists())

    def test_generic_song_without_id_still_queries_official_search(self):
        self.make_cache()
        session = Session([Response({"code": 200, "result": {"songs": [make_track()], "songCount": 1}}), Response({"code": 200, "songs": [make_track()]}), Response({"code": 200, "uncollected": True})])
        with mock.patch.object(source, "CACHE_ROOTS", (self.root,)), mock.patch.object(source, "_authorized_session", return_value=session):
            result = source.acquire_lossless("Test Song Test Artist", self.root / "attempt")
        self.assertTrue(result["cache_reused"])
        self.assertTrue(session.calls[0][0].endswith("/api/search/get"))
        self.assertFalse(any("player/url" in url for url, _ in session.calls))

    def test_title_only_does_not_use_single_cached_artist(self):
        self.make_cache()
        session = Session([Response({"code": 200, "result": {"songs": [make_track()], "songCount": 1}})])
        with mock.patch.object(source, "CACHE_ROOTS", (self.root,)), mock.patch.object(source, "_authorized_session", return_value=session):
            with self.assertRaises(source.NeedsReview):
                source.acquire_lossless("Test Song", self.root / "attempt")
        failure = json.loads((self.root / "attempt" / "source_failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["status"], "needs_review")

    def test_new_download_mocked_end_to_end_without_secrets(self):
        path, track, quality = self.make_flac()
        data = path.read_bytes()
        secret = "TOKEN_MUST_NOT_BE_SAVED"
        api = Session([Response({"code": 200, "songs": [track]}), Response({"code": 200, "data": [{**quality, "url": "https://m1.music.126.net/" + secret, "encodeType": "mp3", "unknown": secret}]}), Response({"code": 200, "lrc": {"lyric": "[00:00.00]作词：作者\n[00:00.25]歌词"}})])
        cdn = Session([Response(headers={"Content-Type": "audio/mpeg"}, chunks=[data[:100], data[100:]])])
        with mock.patch.object(source, "_authorized_session", return_value=api), mock.patch.object(source.requests, "Session", return_value=cdn):
            result = source.acquire_lossless("Test Song Test Artist", self.root / "attempt", 123)
        self.assertFalse(result["cache_reused"])
        self.assertEqual(result["lyrics"]["entries"], [{"time": 0.25, "text": "歌词"}])
        for output in (self.root / "attempt").glob("*.json"):
            self.assertNotIn(secret, output.read_text(encoding="utf-8"))
            self.assertNotIn("Cookie", output.read_text(encoding="utf-8"))
        self.assertTrue(api.closed)
        self.assertTrue(cdn.closed)

    def test_failure_marker_retains_partial_and_redacts_network_error(self):
        _, track, quality = self.make_flac()
        api = Session([Response({"code": 200, "songs": [track]}), Response({"code": 200, "data": [{**quality, "url": "https://m1.music.126.net/TOKEN"}]})])
        cdn = Session([Response(chunks=[b"fLaC", OSError("Cookie=SECRET signed=TOKEN")])])
        with mock.patch.object(source, "_authorized_session", return_value=api), mock.patch.object(source.requests, "Session", return_value=cdn):
            with self.assertRaises(source.SourceError):
                source.acquire_lossless("Test Song", self.root / "attempt", 123)
        failure = (self.root / "attempt" / "source_failure.json").read_text(encoding="utf-8")
        self.assertTrue(json.loads(failure)["partial_file_retained"])
        self.assertNotIn("SECRET", failure)
        self.assertNotIn("TOKEN", failure)
        self.assertEqual((self.root / "attempt" / "source.flac.part").read_bytes(), b"fLaC")
        self.assertFalse((self.root / "attempt" / "source_metadata.json").exists())

    def test_truncated_search_needs_review(self):
        session = Session([Response({"code": 200, "result": {"songs": [make_track()], "songCount": 500}})])
        with mock.patch.object(source, "_authorized_session", return_value=session):
            with self.assertRaises(source.NeedsReview):
                source.acquire_lossless("Test Song Test Artist", self.root / "attempt")

    def test_manifest_cannot_reference_arbitrary_outside_file(self):
        manifest, record, _ = self.make_cache()
        sub = self.root / "sub"
        sub.mkdir()
        with self.assertRaises(source.SourceError) as caught:
            source._reuse_cache(sub / manifest.name, record, record["track"], sub / "copy.part")
        self.assertEqual(caught.exception.code, "cache_path")


if __name__ == "__main__":
    unittest.main()
