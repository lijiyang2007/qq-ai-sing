"""Strict official lossless acquisition; no lossy fallback, transcoding, GPU, or QQ.

Public contract: ``acquire_lossless(query, new_workdir, track_id=None) -> dict``.
The directory must not exist.  A successful result is JSON serializable; a failed
attempt leaves a failure marker and any .part file, never a success manifest.
``NeedsReview`` means the caller must ask a human, not pick the first candidate.
An explicit track_id is treated as a human selection but must still match query.
Without an ID, query must contain the exact title AND complete artist name(s),
e.g. ``我怀念的 孙燕姿`` / ``Love Story - Taylor Swift``.  Title-only searches pause.

Cached official manifests are re-hashed and fully CPU decoded before reuse.  They
are copied, not linked, into a new workdir.  A cache hit is offline: missing lyric
metadata yields available=False, not a network request.  Lyrics are optional
alignment hints, never ground truth for acoustic/singing boundaries.

Dependencies: requests, numpy, soundfile (libsndfile).  Only _ne_session from the
legacy helper is used, lazily, for allowlisted official endpoints.  Never call its
search/download/fallback helpers; never persist credentials or signed asset URLs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import numpy as np
import requests
import soundfile as sf
from . import config

ROOT = Path(__file__).resolve().parents[1]
LEGACY_HELPER = ROOT / "tools" / "song_source.py"
CACHE_ROOTS = (config.DATA_ROOT / "sources_lossless",)
OFFICIAL_ORIGIN = "https://music.163.com"
OFFICIAL_ENDPOINTS = frozenset({
    "/api/search/get", "/api/cloudsearch/pc", "/api/song/detail", "/api/song/red/count", "/api/song/enhance/player/url/v1", "/api/song/lyric",
})
API_TIMEOUT = (15, 45)
DOWNLOAD_TIMEOUT = (20, 45)
DOWNLOAD_BUDGET_SECONDS = 30 * 60  # wall-clock budget, plus at most one read timeout
MAX_SOURCE_BYTES = 2 * 1024 ** 3
DURATION_TOLERANCE_SECONDS = 0.25
UA = "HQCover-LosslessSource/1.0"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_VARIANT = re.compile(
    r"(?i)\b(?:live|karaoke|instrumental|remix|cover|demo|acoustic|sped\s*up|slowed)\b"
    r"|现场|現場|演唱会|演唱會|翻唱|伴奏|消音|混音|纯音乐|純音樂|加速版|慢速版|钢琴版|鋼琴版|吉他版"
)
_CREDIT = re.compile(
    r"(?i)^\s*(?:作词|作詞|作曲|词曲|詞曲|编曲|編曲|编写|制作人|製作人|制作|製作|"
    r"词|詞|曲|演唱|原唱|歌手|发行|發行|出品|监制|監製|录音|錄音|混音|母带|母帶|"
    r"和声|和聲|吉他|贝斯|貝斯|鼓|弦乐|弦樂|钢琴|鋼琴|录音室|錄音室|"
    r"lyrics?|lyricist|composer|composed\s+by|written\s+by|arrang(?:er|ed\s+by)|"
    r"producer|produced\s+by|vocal(?:s)?|mix(?:ed\s+by)?|master(?:ed\s+by)?|"
    r"copyright|制作统筹|製作統籌|OP|SP)(?:\s*[:：/／]|\s+)"
)
_INSTRUMENTAL = re.compile(r"(?i)^\s*(?:纯音乐[，,。\s]*请欣赏|純音樂[，,。\s]*請欣賞|纯音乐|純音樂|instrumental(?:\s+version)?)\s*[。.!！]?\s*$")
_TIMESTAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")


class SourceError(RuntimeError):
    """Sanitized failure.  Its message/code contain no underlying exception text."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class NeedsReview(SourceError):
    """Ambiguous or mismatched identity; candidates are safe, JSON track records."""

    def __init__(self, message: str, candidates: list[dict] | None = None):
        self.candidates = candidates or []
        super().__init__("needs_review", message)


def _require(condition, code: str, message: str) -> None:
    if not condition:
        raise SourceError(code, message)


def _json_write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _normalize(text: str) -> str:
    # Ignore separators/spacing, never remove words such as live or parentheses contents.
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if unicodedata.category(c)[0] in ("L", "N"))


def _track(value: dict) -> dict:
    """Normalize official API or cached track shape without arbitrary metadata."""
    try:
        artists = value.get("artists") or value.get("ar") or []
        album = value.get("album") or value.get("al") or {}
        result = {
            "id": int(value["id"]),
            "title": value.get("title", value.get("name", "")),
            "artists": [{"id": a.get("id"), "name": a.get("name", "")} for a in artists],
            "album": {"id": album.get("id"), "name": album.get("name", "")},
            "duration_ms": int(value.get("duration_ms") or value.get("duration") or value.get("dt") or 0),
        }
        valid = (result["id"] > 0 and isinstance(result["title"], str)
                 and bool(_normalize(result["title"])) and bool(artists)
                 and all(isinstance(a["name"], str) and _normalize(a["name"]) for a in result["artists"])
                 and result["duration_ms"] > 0 and isinstance(result["album"]["name"], str)
                 and all(a["id"] is None or type(a["id"]) is int for a in result["artists"])
                 and (result["album"]["id"] is None or type(result["album"]["id"]) is int))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SourceError("invalid_track", "Official track identity is incomplete.") from None
    _require(valid, "invalid_track", "Official track identity is incomplete.")
    return result


def _identity_match(query: str, track: dict, allow_title_only: bool = False) -> bool:
    query_norm = _normalize(query)
    title = _normalize(track["title"])
    artists = [_normalize(a["name"]) for a in track["artists"]]
    # Exact concatenations handle Chinese and multi-word Latin titles without guessing splits.
    signatures = {title + "".join(artists), "".join(artists) + title}
    if len(artists) == 2:
        signatures.update({title + "".join(reversed(artists)), "".join(reversed(artists)) + title})
    return query_norm in signatures or (allow_title_only and query_norm == title)


def _variant_unrequested(query: str, track: dict) -> bool:
    # Version words in title must have been explicitly typed. Album live markers
    # always pause: exact song+artist text alone does not disambiguate a live album.
    q = unicodedata.normalize("NFKC", query).casefold()
    title_matches = _VARIANT.findall(track["title"])
    if any(marker.casefold() not in q for marker in title_matches):
        return True
    return bool(_VARIANT.search(track["album"]["name"]))


def select_candidate(query: str, candidates: list[dict], track_id: int | None = None, allow_variant: bool = False) -> dict:
    """No fuzzy scoring. Explicit pro variants require exact ID and full canonical identity."""
    _require(type(allow_variant) is bool, "invalid_variant_policy", "Variant policy must be boolean.")
    if allow_variant:
        _require(type(track_id) is int and track_id > 0, "invalid_track_id", "Explicit variant selection requires a positive track ID.")
    tracks = []
    for item in candidates:
        try:
            normalized = _track(item)
        except SourceError:
            continue
        if normalized["id"] not in {t["id"] for t in tracks}:
            tracks.append(normalized)
    if track_id is not None:
        matches = [t for t in tracks if t["id"] == track_id and _identity_match(query, t, not allow_variant)]
    else:
        matches = [t for t in tracks if _identity_match(query, t)]
    if len(matches) != 1:
        raise NeedsReview("Specify an exact title and artist, or review and supply a matching track ID.", tracks[:20])
    chosen = matches[0]
    if not allow_variant and _variant_unrequested(query, chosen):
        raise NeedsReview("The candidate is a live/cover/karaoke or alternate version; review explicitly.", [chosen])
    return chosen


def _authorized_session():
    # Import only this known local helper; never read or log the cookie ourselves.
    try:
        spec = importlib.util.spec_from_file_location("_hq_cover_authorized_source", LEGACY_HELPER)
        _require(spec is not None and spec.loader is not None, "authorization", "Authorized helper is unavailable.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._ne_session()
    except SourceError:
        raise
    except Exception:
        raise SourceError("authorization", "Authorized session could not be initialized.") from None


def _cookie_configured():
    """是否已经放了自己的网易云 Cookie（没放就只能走公开接口，无损源会失败）。

    路径统一由 config.NETEASE_COOKIE 决定，不在这里自己拼。
    """
    cookie = Path(config.NETEASE_COOKIE)
    try:
        data = json.loads(cookie.read_text(encoding="utf-8-sig"))
        return isinstance(data, dict) and bool(data)
    except Exception:
        return False


def _official_get(session, endpoint: str, params: dict) -> dict:
    _require(endpoint in OFFICIAL_ENDPOINTS, "api_endpoint", "Official endpoint is not allowlisted.")
    response = None
    try:
        # Redirects disabled: an authorization Cookie must never follow a CDN or third party.
        response = session.get(OFFICIAL_ORIGIN + endpoint, params=params, timeout=API_TIMEOUT, allow_redirects=False)
        _require(response.status_code == 200, "api_http", "Official API returned an unsuccessful HTTP status.")
        payload = response.json()
        _require(isinstance(payload, dict) and payload.get("code") == 200, "api_denied", "Official API denied or did not fulfill this request.")
        return payload
    except SourceError:
        raise
    except Exception:
        raise SourceError("api_network", "Official API request failed; diagnostic details suppressed.") from None
    finally:
        if response is not None:
            response.close()


def _valid_cdn(url: str) -> bool:
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        return (p.scheme in ("http", "https") and not p.username and not p.password
                and p.port in (None, 80, 443) and not p.fragment
                and any(host.endswith(s) for s in (".music.126.net", ".music.163.com")))
    except (TypeError, ValueError):
        return False


def _quality(data: dict, track_id: int) -> dict:
    _require(data.get("id") == track_id and data.get("code") == 200,
             "stream_denied", "Official lossless stream is not available for this track.")
    _require(data.get("level") == "lossless" and str(data.get("type", "")).lower() == "flac",
             "not_lossless", "Official lossless FLAC is required; lossy fallback is forbidden.")
    trial_privilege = data.get("freeTrialPrivilege") or {}
    _require(isinstance(trial_privilege, dict), "stream_response", "Malformed trial restriction metadata.")
    _require(data.get("freeTrialInfo") is None and not trial_privilege.get("resConsumable", False),
             "trial_restricted", "Trial-restricted audio is not accepted.")
    _require(type(data.get("size")) is int and 42 <= data["size"] <= MAX_SOURCE_BYTES,
             "invalid_size", "Official file size is missing or outside safety bounds.")
    _require(isinstance(data.get("md5"), str) and bool(_HEX32.fullmatch(data["md5"])),
             "missing_checksum", "Official file MD5 is required.")
    # Whitelist scalar fields; URL/freeTrialInfo/unknown keys never reach a manifest.
    result = {"id": track_id, "code": 200, "level": "lossless", "type": "flac",
              "size": data["size"], "md5": data["md5"].lower()}
    for key in ("sr", "sampleRate", "bitDepth", "bitdepth", "time", "br"):
        if type(data.get(key)) is int and data[key] > 0:
            result[key] = data[key]
    return result


def _stream_blocks(response):
    # iter_content(256 KiB) can wait indefinitely for one chunk if a peer sends a
    # byte just before each inactivity timeout. read1 returns after one underlying
    # read, allowing the wall-clock budget to be checked between small arrivals.
    read1 = getattr(getattr(response, "raw", None), "read1", None)
    if callable(read1):
        while True:
            block = read1(256 * 1024, decode_content=False)
            if not block:
                return
            yield block
    else:
        # Older urllib3 has no read1; one-byte iteration sacrifices speed rather
        # than silently removing the bounded-time guarantee.
        yield from response.iter_content(chunk_size=1)


def _download(url: str, destination: Path, quality: dict) -> None:
    _require(_valid_cdn(url), "cdn_url", "Official CDN address is missing or outside approved domains.")
    deadline = time.monotonic() + DOWNLOAD_BUDGET_SECONDS
    response = None
    session = requests.Session()  # never the authorized API session
    # trust_env=False disables .netrc authentication and inherited proxies/credentials.
    session.trust_env = False
    session.auth = None
    session.headers.clear()
    session.headers.update({"User-Agent": UA, "Accept-Encoding": "identity"})
    try:
        for _ in range(4):
            _require(time.monotonic() < deadline, "download_budget", "Download exceeded the bounded time budget.")
            session.cookies.clear()  # do not propagate even CDN Set-Cookie headers on redirects
            response = session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            next_url = urljoin(url, response.headers.get("Location", ""))
            response.close()
            response = None
            _require(_valid_cdn(next_url), "cdn_redirect", "CDN redirect left approved music domains.")
            _require(not (urlsplit(url).scheme == "https" and urlsplit(next_url).scheme != "https"),
                     "cdn_redirect", "CDN redirect attempted an HTTPS downgrade.")
            url = next_url
        _require(response is not None and response.status_code == 200, "cdn_http", "CDN download returned an unsuccessful status.")
        _require(response.headers.get("Content-Encoding", "identity").lower() in ("identity", ""),
                 "cdn_encoding", "Unexpected transfer encoding.")
        length = response.headers.get("Content-Length")
        _require(length is None or (str(length).isdigit() and int(length) == quality["size"]),
                 "size_mismatch", "CDN length differs from the official file size.")
        size = 0
        with destination.open("xb") as handle:
            for block in _stream_blocks(response):
                _require(time.monotonic() < deadline, "download_budget", "Download exceeded the bounded time budget.")
                if block:
                    size += len(block)
                    _require(size <= quality["size"], "size_mismatch", "Download exceeds official file size.")
                    handle.write(block)
        _require(time.monotonic() < deadline, "download_budget", "Download exceeded the bounded time budget.")
        _require(size == quality["size"], "size_mismatch", "Download is incomplete or has a different size.")
    except SourceError:
        raise
    except Exception:
        raise SourceError("download_failed", "CDN download failed; diagnostic details suppressed.") from None
    finally:
        if response is not None:
            response.close()
        session.close()


def validate_lossless(path: Path, track: dict, quality: dict, expected_sha256: str | None = None) -> dict:
    """Re-hash and fully decode FLAC, including interleaved signed 16/24-bit PCM MD5.

    MIME, extension and API encodeType are deliberately not used for codec identity.
    For PCM_24, libsndfile returns left-justified int32; shift then pack low 3 bytes
    little-endian, per FLAC STREAMINFO MD5 specification (including sign samples).
    """
    file_md5, sha256 = hashlib.md5(), hashlib.sha256()
    size = 0
    try:
        with Path(path).open("rb") as handle:
            header = handle.read(42)
            handle.seek(0)
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(block)
                _require(size <= quality["size"], "size_mismatch", "Source is larger than official metadata.")
                file_md5.update(block)
                sha256.update(block)
        _require(size == quality["size"], "size_mismatch", "Source size differs from official metadata.")
        _require(file_md5.hexdigest() == quality["md5"].lower(), "file_md5", "Source differs from the official file MD5.")
        if expected_sha256 is not None:
            _require(bool(_HEX64.fullmatch(expected_sha256)) and sha256.hexdigest() == expected_sha256.lower(),
                     "cache_sha256", "Cached source SHA-256 verification failed.")
        _require(len(header) == 42 and header[:4] == b"fLaC" and header[4] & 0x7F == 0
                 and int.from_bytes(header[5:8], "big") == 34,
                 "flac_header", "Native FLAC STREAMINFO is required.")
        info = header[8:42]
        packed = int.from_bytes(info[10:18], "big")
        sr = (packed >> 44) & 0xFFFFF
        channels = ((packed >> 41) & 7) + 1
        bits = ((packed >> 36) & 31) + 1
        frames = packed & 0xFFFFFFFFF
        pcm_md5 = info[18:34].hex()
        _require(sr > 0 and frames > 0 and bits in (16, 24) and pcm_md5 != "0" * 32,
                 "flac_streaminfo", "FLAC must contain sample count and PCM checksum, with 16/24-bit PCM.")
        _require(abs(frames / sr - track["duration_ms"] / 1000) <= DURATION_TOLERANCE_SECONDS,
                 "duration_mismatch", "FLAC duration does not match the selected official track.")
        for name in ("sr", "sampleRate"):
            _require(not quality.get(name) or quality[name] == sr, "sample_rate_mismatch", "Official and measured sample rates differ.")
        for name in ("bitDepth", "bitdepth"):
            _require(not quality.get(name) or quality[name] == bits, "bit_depth_mismatch", "Official and measured bit depths differ.")
        if quality.get("time"):
            _require(abs(frames / sr - quality["time"] / 1000) <= DURATION_TOLERANCE_SECONDS,
                     "duration_mismatch", "FLAC duration differs from the official stream duration.")
        decoded_md5 = hashlib.md5()
        decoded_frames = 0
        with sf.SoundFile(str(path)) as audio:
            _require(audio.format == "FLAC" and audio.subtype == f"PCM_{bits}"
                     and audio.samplerate == sr and audio.channels == channels and len(audio) == frames,
                     "decoder_metadata", "Decoder and FLAC STREAMINFO disagree.")
            for block in audio.blocks(blocksize=65536, dtype="int16" if bits == 16 else "int32", always_2d=True):
                if bits == 16:
                    raw = block.astype("<i2", copy=False).tobytes(order="C")
                else:
                    pcm = (block >> 8).astype("<i4", copy=False)
                    raw = pcm.view(np.uint8).reshape(-1, 4)[:, :3].tobytes(order="C")
                decoded_md5.update(raw)
                decoded_frames += len(block)
        _require(decoded_frames == frames, "incomplete_decode", "Full FLAC decode did not produce the expected sample count.")
        _require(decoded_md5.hexdigest() == pcm_md5, "pcm_md5", "Decoded PCM checksum differs from FLAC STREAMINFO.")
        return {
            "download": {"bytes": size, "md5": file_md5.hexdigest(), "sha256": sha256.hexdigest(),
                         "official_size_match": True, "official_md5_match": True},
            "codec": {"format": "FLAC", "bits_per_sample": bits, "sample_rate_hz": sr,
                      "channels": channels, "total_samples": frames, "duration_seconds": frames / sr,
                      "streaminfo_pcm_md5": pcm_md5},
            "verification": {"full_cpu_decode": True, "decoded_frames": decoded_frames,
                             "decoded_pcm_md5": decoded_md5.hexdigest(), "flac_streaminfo_pcm_md5_match": True,
                             "gpu_used": False, "audio_transcoded_or_resampled": False},
        }
    except SourceError:
        raise
    except Exception:
        raise SourceError("validation_failed", "Lossless integrity validation failed; diagnostic details suppressed.") from None


def parse_lrc(lrc: str, track_id: int, instrumental: bool = False) -> dict:
    """Parse line timestamps; remove credits, respect offset, retain lyric repeats."""
    _require(isinstance(lrc, str), "invalid_lyrics", "LRC must be text.")
    entries = []
    offset_match = re.search(r"(?i)\[offset:([+-]?\d+)\]", lrc)
    offset = int(offset_match.group(1)) / 1000 if offset_match else 0.0
    is_instrumental = bool(instrumental)
    seen = set()
    for line in lrc.splitlines():
        stamps = list(_TIMESTAMP.finditer(line))
        text = _TIMESTAMP.sub("", line).strip()
        text = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", text).strip()
        if _INSTRUMENTAL.fullmatch(text):
            is_instrumental = True
            continue
        if not stamps or not text or _CREDIT.match(text):
            continue
        for stamp in stamps:
            fraction = stamp.group(3) or "0"
            seconds = int(stamp.group(1)) * 60 + int(stamp.group(2)) + int(fraction) / (10 ** len(fraction)) + offset
            # Negative adjusted times are clamped to track start, not made into a negative boundary.
            seconds = round(max(0.0, seconds), 6)
            key = (seconds, text)
            if key not in seen:
                entries.append({"time": seconds, "text": text})
                seen.add(key)
    entries.sort(key=lambda entry: entry["time"])
    return {"entries": entries, "source": "netease", "track_id": track_id,
            "instrumental": is_instrumental, "available": bool(entries) or is_instrumental,
            "timing_role": "auxiliary_only_not_acoustic_boundaries"}


def _cached_lyrics(value, track_id: int) -> dict:
    result = parse_lrc("", track_id)
    if not isinstance(value, dict) or value.get("track_id") != track_id or value.get("source") != "netease":
        return result
    result["instrumental"] = value.get("instrumental") is True
    for item in value.get("entries", []) if isinstance(value.get("entries"), list) else []:
        if not isinstance(item, dict):
            continue
        t, text = item.get("time"), item.get("text")
        if (type(t) in (float, int) and math.isfinite(t) and t >= 0 and isinstance(text, str)
                and text.strip() and not _CREDIT.match(text)):
            result["entries"].append({"time": float(t), "text": text})
    result["entries"].sort(key=lambda entry: entry["time"])
    result["available"] = bool(result["entries"]) or result["instrumental"]
    return result


def _lyrics(session, track_id: int) -> dict:
    try:
        payload = _official_get(session, "/api/song/lyric", {"id": track_id, "lv": -1, "kv": -1, "tv": -1})
        # 'uncollected' means missing lyrics, not instrumental. 'nolyric' is explicit.
        return parse_lrc((payload.get("lrc") or {}).get("lyric", ""), track_id,
                         instrumental=payload.get("nolyric") is True)
    except (SourceError, TypeError, AttributeError):
        result = parse_lrc("", track_id)
        result["unavailable_reason"] = "official_lyrics_unavailable"
        return result


def _cache_records() -> list[tuple[Path, dict, dict]]:
    records = []
    for root in CACHE_ROOTS:
        if not root.is_dir():
            continue
        for manifest in sorted(root.rglob("source_metadata.json")):
            try:
                if manifest.stat().st_size > 2 * 1024 * 1024:
                    continue
                with manifest.open(encoding="utf-8") as handle:
                    record = json.load(handle)
                track = _track(record.get("track") or {})
                if record.get("status") != "verified_official_lossless":
                    continue
                if record.get("source") not in ("netease", "NetEase Cloud Music official API"):
                    continue
                records.append((manifest, record, track))
            except (OSError, ValueError, TypeError, AttributeError, SourceError):
                continue
    return records


def _reuse_cache(manifest: Path, record: dict, track: dict, destination: Path) -> tuple[dict, dict, dict]:
    _require(record.get("transcoded") is False and record.get("trial_restricted") is False,
             "cache_provenance", "Cache is not a verified original non-trial source.")
    quality = _quality(record.get("quality_response") or {}, track["id"])
    expected_sha = record.get("source_sha256") or (record.get("download") or {}).get("sha256")
    _require(isinstance(expected_sha, str) and bool(_HEX64.fullmatch(expected_sha)),
             "cache_sha256", "Verified cache manifest requires a SHA-256.")
    raw_path = record.get("source_path") or record.get("download_path")
    _require(isinstance(raw_path, str) and bool(raw_path), "cache_path", "Cache source path is missing.")
    source = Path(raw_path)
    source = source.resolve() if source.is_absolute() else (manifest.parent / source).resolve()
    # A manifest can only reuse assets within its own directory, never arbitrary paths.
    _require(source.is_relative_to(manifest.parent.resolve()) and source.is_file(),
             "cache_path", "Cache source is missing or outside its manifest directory.")
    verified = validate_lossless(source, track, quality, expected_sha)
    # Exclusive physical copy, then re-hash/full-decode copy to avoid stale or changing cache races.
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            copied = 0
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                copied += len(chunk)
                _require(copied <= quality["size"], "cache_changed", "Cache changed during copying.")
                dst.write(chunk)
    except SourceError:
        raise
    except Exception:
        raise SourceError("cache_copy", "Verified cache could not be copied.") from None
    verified = validate_lossless(destination, track, quality, expected_sha)
    return quality, verified, _cached_lyrics(record.get("lyrics"), track["id"])


def acquire_lossless(query: str, workdir: Path, track_id: int | None = None, allow_variant: bool = False) -> dict:
    """Acquire a strictly matched official FLAC or verified cache into a NEW directory.

    Raises NeedsReview for ambiguity, SourceError for all operational failures.
    No exception text from requests/decoder/OS is persisted or propagated.  No
    automatic retry, paid upgrade, unauthorized URL, or lossy fallback is attempted.
    """
    _require(isinstance(query, str) and bool(_normalize(query)), "invalid_query", "A song title and artist are required.")
    _require(track_id is None or (type(track_id) is int and track_id > 0), "invalid_track_id", "Track ID must be a positive integer.")
    _require(type(allow_variant) is bool, "invalid_variant_policy", "Variant policy must be boolean.")
    if allow_variant:
        _require(type(track_id) is int and track_id > 0, "invalid_track_id", "Explicit variant selection requires a positive track ID.")
    workdir = Path(workdir)
    try:
        workdir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SourceError("workdir_exists", "Workdir already exists; refusing to overwrite or mix attempts.") from None
    except OSError:
        raise SourceError("workdir_create", "An exclusive new workdir could not be created.") from None
    session = None
    partial = workdir / "source.flac.part"
    final = workdir / "source.flac"
    metadata = workdir / "source_metadata.json"
    try:
        _json_write(workdir / "acquisition_started.json", {
            "status": "started", "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "notice": "Incomplete until source_metadata.json says verified_official_lossless.",
        })
        caches = _cache_records()
        # Title-only input must not silently infer the artist from the one cached song.
        cache_matches = [item for item in caches if (track_id is None or item[2]["id"] == track_id)
                         and _identity_match(query, item[2], allow_title_only=track_id is not None and not allow_variant)]
        cache_match = None
        if cache_matches:
            chosen = select_candidate(query, [x[2] for x in cache_matches], track_id, allow_variant=allow_variant)
            cache_match = next(x for x in cache_matches if x[2]["id"] == chosen["id"])
        # Even a full title+artist cache match could hide another release.  An
        # explicit ID is authoritative; everything else queries official search
        # so that no particular track ID is baked into this repository.
        if cache_match and track_id is not None:
            track = cache_match[2]
            quality, checked, lyrics = _reuse_cache(cache_match[0], cache_match[1], track, partial)
            reused = True
        else:
            session = _authorized_session()
            if track_id is None:
                payload = _official_get(session, "/api/search/get", {"s": query, "type": 1, "limit": 100, "offset": 0})
                result = payload.get("result") or {}
                candidates = result.get("songs") or []
                # 搜索结果被截断（songCount > 返回条数）时，**不直接放弃**：
                # 热门歌这个比例很高（《晴天》248 条、《起风了》300 条），直接 NeedsReview
                # 等于命令行点歌几乎必须手带 --track-id。
                #
                # 但也不能从这 100 条里瞎挑一个 —— 那正是原设计要防的"选错版本"。
                # 所以改走 resolve_auto_original()：它在**完整候选集**上按
                # 「官方原版徽标优先，其次精确红心收藏数最高」选版，选不出来就仍然
                # 抛 NeedsReview。既不放宽判定标准，也不让用户被搜索结果条数卡住。
                if result.get("songCount", len(candidates)) > len(candidates):
                    from .song_resolver import resolve_auto_original
                    resolved = resolve_auto_original(query)
                    if not resolved.get("ok"):
                        raise NeedsReview(
                            "搜索结果不完整，且无法自动确定唯一原版；请用准确歌曲 ID 指定。",
                            [{"id": resolved.get("track_id"), "title": resolved.get("title"),
                              "artists": [{"name": a} for a in (resolved.get("artists") or [])],
                              "album": {"name": resolved.get("album", "")}}]
                            if resolved.get("track_id") else [],
                        )
                    # 只取 ID，后面统一走 /api/song/detail 重新取完整记录，
                    # 避免两处 track 结构不一致（resolver 返回的是 _public() 投影）。
                    track_id = int(resolved["track_id"])
                else:
                    track = select_candidate(query, candidates, allow_variant=allow_variant)
                    track_id = track["id"]
            detail = _official_get(session, "/api/song/detail", {"id": track_id, "ids": json.dumps([track_id])})
            track = select_candidate(query, detail.get("songs") or [], track_id, allow_variant=allow_variant)
            matching_cache = next((item for item in caches if item[2] == track), None)
            if matching_cache:
                quality, checked, lyrics = _reuse_cache(matching_cache[0], matching_cache[1], track, partial)
                reused = True
                if not lyrics["available"]:
                    lyrics = _lyrics(session, track_id)
            else:
                payload = _official_get(session, "/api/song/enhance/player/url/v1", {
                    "ids": json.dumps([track_id]), "level": "lossless", "encodeType": "flac",
                })
                rows = payload.get("data") or []
                _require(len(rows) == 1, "stream_response", "Expected exactly one official stream response.")
                stream = rows[0]
                quality = _quality(stream, track_id)
                _download(stream.get("url"), partial, quality)
                checked = validate_lossless(partial, track, quality)
                lyrics = _lyrics(session, track_id)
                reused = False
        # Keep the exclusively-created file's identity while promoting the validated
        # result; Windows rename refuses existing targets. The private directory is ours.
        _require(not final.exists(), "output_exists", "Final source unexpectedly exists.")
        partial.rename(final)
        result = {
            "schema_version": 1, "status": "verified_official_lossless", "source": "netease",
            "source_path": str(final.resolve()), "source_sha256": checked["download"]["sha256"],
            "source_metadata_path": str(metadata.resolve()), "track": track, "lyrics": lyrics,
            "quality_request": {"level": "lossless", "encodeType": "flac"}, "quality_response": quality,
            "trial_restricted": False, "transcoded": False, "cache_reused": reused,
            "allow_variant": allow_variant,
            "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
            "limitations": "Official file and decoded PCM verified; historical recording/master lineage is not independently provable.",
            **checked,
        }
        _json_write(metadata, result)
        return result
    except Exception as error:
        safe_error = error if isinstance(error, SourceError) else SourceError("source_failed", "Source acquisition failed; diagnostic details suppressed.")
        failure = {"status": "needs_review" if isinstance(safe_error, NeedsReview) else "failed",
                   "error_code": safe_error.code, "error": str(safe_error),
                   "partial_file_retained": partial.exists() or final.exists(), "retry_performed": False,
                   "notice": "Any retained file is unusable until successful verified metadata is produced."}
        if isinstance(safe_error, NeedsReview):
            failure["candidates"] = safe_error.candidates
        try:
            _json_write(workdir / "source_failure.json", failure)
        except Exception:
            pass  # preserve original sanitized error even when disk writes fail
        raise safe_error from None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
