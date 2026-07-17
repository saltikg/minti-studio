import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import current_app

from app.video_shorts.config import FFMPEG_TIMEOUT, OPENAI_MODEL, WHISPER_MODEL, _openai_client
from app.video_shorts.services.db import _ensure_transcript_schema
from app.video_shorts.services.media_utils import _extract_audio_segment, _resolve_ffmpeg
from app.video_shorts.services.transcript_lang_tagging import infer_lang_from_text, tag_segments_with_language
import re


_EN_STOPWORDS = {
    "the", "and", "to", "of", "in", "is", "it", "that", "for", "on", "with",
    "as", "are", "was", "be", "this", "you", "we", "they", "have", "from",
    "at", "or", "not", "but", "your", "our", "their", "can", "will",
}
_TR_STOPWORDS = {
    "ve", "bir", "bu", "ile", "için", "ama", "gibi", "çok", "daha", "de",
    "da", "mi", "mı", "mu", "mü", "sen", "ben", "biz", "siz", "onlar",
}
_MIN_BOUNDARY_CUE_SECONDS = 0.7


def _normalize_whisper_language(raw: Any) -> str:
    val = str(raw or "").strip().lower()
    if not val:
        return ""
    if val.startswith("en") or val in {"english"}:
        return "en"
    if val.startswith("tr") or val in {"turkish", "turkce"}:
        return "tr"
    if val.startswith("ar") or val in {"arabic"}:
        return "ar"
    return val


def _extract_transcription_language(resp: Any) -> str:
    # OpenAI client objects may expose fields via attributes, dicts, or model_dump.
    raw = getattr(resp, "language", None)
    if raw:
        return _normalize_whisper_language(raw)
    if isinstance(resp, dict):
        return _normalize_whisper_language(resp.get("language"))
    try:
        dumped = resp.model_dump()  # pydantic-style response object
    except Exception:
        dumped = None
    if isinstance(dumped, dict):
        return _normalize_whisper_language(dumped.get("language"))
    return ""


def _looks_english_text(text: str) -> bool:
    if not text:
        return False
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z']+", text)]
    if len(tokens) < 6:
        return False
    en_hits = sum(1 for t in tokens if t in _EN_STOPWORDS)
    tr_hits = sum(1 for t in tokens if t in _TR_STOPWORDS)
    tr_chars = sum(1 for ch in text if ch in "çğıöşüÇĞİÖŞÜ")
    return tr_chars == 0 and en_hits >= 2 and en_hits >= (tr_hits + 1)


def _detect_primary_audio_language(video_path: Path) -> str:
    """
    Fast language gate before heavy transcript logic.
    Returns 'en', 'tr', or '' (unknown/auto).
    """
    if not _openai_client or not video_path.exists():
        return ""
    try:
        def _log_probe_result(result: str, path_label: str, votes: List[str], duration_seconds: Optional[float]) -> None:
            try:
                current_app.logger.info(
                    "lang_probe result=%s path=%s votes=%s duration=%.1f",
                    result,
                    path_label,
                    votes,
                    float(duration_seconds) if duration_seconds is not None else -1.0,
                )
            except Exception:
                pass

        def _probe_window_language(start_seconds: float, duration_seconds: float) -> str:
            probe_path: Optional[Path] = None
            try:
                end_seconds = start_seconds + duration_seconds
                probe_path = _extract_audio_segment(video_path, start_seconds, end_seconds)
                with probe_path.open("rb") as f:
                    resp = _openai_client.audio.transcriptions.create(
                        model=WHISPER_MODEL,
                        file=f,
                        response_format="verbose_json",
                    )
                lang = _extract_transcription_language(resp)
                probe_text, _ = _whisper_response_to_segments(resp)
                if lang in {"en", "tr"}:
                    return lang
                if _looks_english_text(probe_text):
                    return "en"
                inferred = infer_lang_from_text(probe_text or "")
                if inferred == "tr":
                    return "tr"
            except Exception as exc:
                try:
                    current_app.logger.warning(
                        "Language probe window failed at %.1fs for %.1fs; ignoring window: %s",
                        start_seconds,
                        duration_seconds,
                        exc,
                    )
                except Exception:
                    pass
            finally:
                if probe_path and probe_path.exists():
                    try:
                        probe_path.unlink()
                    except Exception:
                        pass
            return ""

        duration_seconds: Optional[float] = None
        try:
            ffmpeg_bin = Path(_resolve_ffmpeg())
            ffprobe_bin = ffmpeg_bin.with_name("ffprobe")
            try:
                current_app.logger.info(
                    "lang_probe ffmpeg_resolved=%r ffprobe_bin=%r exists=%s",
                    str(ffmpeg_bin),
                    str(ffprobe_bin),
                    ffprobe_bin.exists(),
                )
            except Exception:
                pass
            ffprobe_cmd = [
                str(ffprobe_bin if ffprobe_bin.exists() else "ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
            try:
                current_app.logger.info("lang_probe ffprobe_cmd=%s", ffprobe_cmd)
            except Exception:
                pass
            proc = subprocess.run(
                ffprobe_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=max(30, int(FFMPEG_TIMEOUT) if FFMPEG_TIMEOUT else 30),
            )
            try:
                current_app.logger.info(
                    "lang_probe ffprobe rc=%s stdout=%r stderr=%r",
                    proc.returncode,
                    (proc.stdout or "").strip(),
                    (proc.stderr or "").strip()[:500],
                )
            except Exception:
                pass
            if proc.returncode == 0:
                raw_duration = (proc.stdout or "").strip()
                if raw_duration:
                    duration_seconds = float(raw_duration)
        except Exception as e:
            try:
                current_app.logger.warning("lang_probe ffprobe exception: %r", e)
            except Exception:
                pass
            duration_seconds = None
        try:
            current_app.logger.info("lang_probe parsed_duration=%r", duration_seconds)
        except Exception:
            pass

        if not duration_seconds or duration_seconds < 90.0:
            # Short clips keep the original single-window behavior.
            result = _probe_window_language(0.0, 45.0)
            _log_probe_result(result, "single_window", [result], duration_seconds)
            return result

        window_duration = 30.0
        max_start = max(duration_seconds - window_duration, 0.0)
        votes: List[str] = []
        for fraction in (0.20, 0.50, 0.80):
            start_seconds = min(max(duration_seconds * fraction, 0.0), max_start)
            lang = _probe_window_language(start_seconds, window_duration)
            if lang in {"en", "tr"}:
                votes.append(lang)

        en_votes = sum(1 for lang in votes if lang == "en")
        tr_votes = sum(1 for lang in votes if lang == "tr")
        if en_votes > tr_votes:
            _log_probe_result("en", "multi_window", votes, duration_seconds)
            return "en"
        if tr_votes > en_votes:
            _log_probe_result("tr", "multi_window", votes, duration_seconds)
            return "tr"
    except Exception as exc:
        try:
            current_app.logger.warning("Language probe failed; fallback to auto mode: %s", exc)
        except Exception:
            pass
    try:
        current_app.logger.info(
            "lang_probe result=%s path=%s votes=%s duration=%.1f",
            "",
            "multi_window" if 'votes' in locals() else "single_window",
            votes if 'votes' in locals() else [],
            float(duration_seconds) if 'duration_seconds' in locals() and duration_seconds is not None else -1.0,
        )
    except Exception:
        pass
    return ""


def _split_text_into_chunks(text: str, max_words: int = 10) -> List[str]:
    if not text:
        return []
    tokens = re.findall(r"\S+", text)
    chunks: List[str] = []
    for start in range(0, len(tokens), max_words):
        chunk = " ".join(tokens[start : start + max_words])
        chunks.append(chunk)
    return chunks


def _chunked_subtitle_intervals(rel_start: float, rel_end: float, chunk_count: int) -> List[Tuple[float, float]]:
    if chunk_count <= 0:
        return []
    span = max(rel_end - rel_start, 0.05)
    per_chunk = span / chunk_count
    per_chunk = max(per_chunk, 0.05)
    intervals: List[Tuple[float, float]] = []
    current_start = rel_start
    for idx in range(chunk_count):
        if idx == chunk_count - 1:
            current_end = rel_end
        else:
            current_end = min(rel_end, current_start + per_chunk)
        if current_end <= current_start:
            current_end = current_start + 0.01
        intervals.append((current_start, current_end))
        current_start = current_end
    return intervals


def _seg_val(seg: Any, key: str, default=None):
    if isinstance(seg, dict):
        return seg.get(key, default)
    try:
        val = getattr(seg, key)
    except Exception:
        return default
    return val if val is not None else default


def _chunk_text_entries(text: str, rel_start: float, rel_end: float, max_words: int = 10) -> List[Tuple[float, float, str]]:
    chunks = _split_text_into_chunks(text, max_words=max_words)
    if not chunks:
        return []
    intervals = _chunked_subtitle_intervals(rel_start, rel_end, len(chunks))
    entries: List[Tuple[float, float, str]] = []
    for (start, end), chunk in zip(intervals, chunks):
        entries.append((start, end, chunk))
    return entries


def _trim_text_to_segment_overlap(
    text: str,
    *,
    segment_start: float,
    segment_end: float,
    overlap_start: float,
    overlap_end: float,
) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    tokens = re.findall(r"\S+", text)
    if not tokens:
        return ""
    full_duration = max(float(segment_end) - float(segment_start), 0.0)
    overlap_duration = max(float(overlap_end) - float(overlap_start), 0.0)
    if full_duration <= 0.0 or overlap_duration <= 0.0:
        return ""
    if overlap_start <= segment_start and overlap_end >= segment_end:
        return text

    token_count = len(tokens)
    start_ratio = min(max((float(overlap_start) - float(segment_start)) / full_duration, 0.0), 1.0)
    end_ratio = min(max((float(overlap_end) - float(segment_start)) / full_duration, 0.0), 1.0)
    start_index = min(token_count - 1, max(0, int(start_ratio * token_count)))
    end_index = min(token_count, max(start_index + 1, int(round(end_ratio * token_count))))
    trimmed = tokens[start_index:end_index]
    if not trimmed:
        keep_words = max(1, min(token_count, int(round(token_count * (overlap_duration / full_duration)))))
        trimmed = tokens[-keep_words:] if overlap_start > segment_start else tokens[:keep_words]
    return " ".join(trimmed).strip()


def _build_srt_for_clip(segments: List[Dict[str, Any]], clip_start: float, clip_end: float) -> Path | None:
    """
    Build a temp SRT file for segments within [clip_start, clip_end].
    Returns path or None if no lines.
    """
    lines = []

    def fmt(ts: float) -> str:
        hrs = int(ts // 3600)
        ts -= hrs * 3600
        mins = int(ts // 60)
        ts -= mins * 60
        secs = int(ts)
        ms = int(round((ts - secs) * 1000))
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    idx = 1
    for seg in segments:
        try:
            s = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        try:
            d = seg.get("duration")
            d = float(d) if d is not None else None
        except Exception:
            d = None
        try:
            e_val = seg.get("end")
            e = float(e_val) if e_val is not None else None
        except Exception:
            e = None
        if e is None:
            e = s + max(d or 0.0, 0.0)
        if d is None:
            d = max(e - s, 0.0)
        if e <= clip_start or s >= clip_end:
            continue
        overlap_start = max(s, clip_start)
        overlap_end = min(e, clip_end)
        overlap_duration = max(overlap_end - overlap_start, 0.0)
        rel_start = max(0.0, overlap_start - clip_start)
        rel_end = max(rel_start + 0.1, overlap_end - clip_start)
        text = (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()
        if not text:
            continue
        is_boundary_segment = s < clip_start or e > clip_end
        if is_boundary_segment:
            text = _trim_text_to_segment_overlap(
                text,
                segment_start=s,
                segment_end=e,
                overlap_start=overlap_start,
                overlap_end=overlap_end,
            )
            if not text or overlap_duration < _MIN_BOUNDARY_CUE_SECONDS:
                continue
        entries = _chunk_text_entries(text, rel_start, rel_end, max_words=10)
        for start, end, chunk_text in entries:
            if is_boundary_segment and (end - start) < _MIN_BOUNDARY_CUE_SECONDS:
                continue
            lines.append(f"{idx}")
            lines.append(f"{fmt(start)} --> {fmt(end)}")
            lines.append(chunk_text)
            lines.append("")
            idx += 1

    if not lines:
        return None

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".srt")
    tmp_path = Path(tmp.name)
    tmp.write("\n".join(lines).encode("utf-8"))
    tmp.close()
    return tmp_path


def _build_srt_from_text(text: str, clip_start: float, clip_end: float) -> Path | None:
    """
    Build a temp SRT file using the provided text for the clip duration.
    """
    if not text:
        return None

    def fmt(ts: float) -> str:
        hrs = int(ts // 3600)
        ts -= hrs * 3600
        mins = int(ts // 60)
        ts -= mins * 60
        secs = int(ts)
        ms = int(round((ts - secs) * 1000))
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    duration = max((clip_end - clip_start), 0.1)
    lines = [
        "1",
        f"{fmt(0.0)} --> {fmt(duration)}",
        text.strip(),
        "",
    ]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".srt")
    tmp_path = Path(tmp.name)
    tmp.write("\n".join(lines).encode("utf-8"))
    tmp.close()
    return tmp_path


def build_transcript_for_range(segments: List[Dict[str, Any]], start: float, end: float, prefer_tr: bool = True) -> str:
    """
    Join segment texts that overlap [start, end]. Text preference:
    if prefer_tr: tr_text or text or ar_text; else: text (fallback ar_text).
    """
    if not segments:
        return ""
    try:
        start = float(start)
    except Exception:
        start = 0.0
    try:
        end = float(end)
    except Exception:
        end = start
    texts = []
    for seg in segments:
        try:
            seg_start = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        seg_dur_val = seg.get("duration")
        seg_end_val = seg.get("end")
        try:
            seg_dur = float(seg_dur_val) if seg_dur_val is not None else None
        except Exception:
            seg_dur = None
        try:
            seg_end = float(seg_end_val) if seg_end_val is not None else None
        except Exception:
            seg_end = None
        if seg_end is None:
            seg_end = seg_start + max(seg_dur or 0.0, 0.0)
        if seg_end <= start or seg_start >= end:
            continue
        if prefer_tr:
            txt = (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()
        else:
            txt = (seg.get("text") or seg.get("ar_text") or "").strip()
        if txt:
            texts.append(txt)
    return " ".join(texts).strip()


def _prepare_audio_for_whisper(src: Path, size_limit_mb: int = 23) -> Path:
    """
    Whisper API has a 25MB file limit and only accepts certain audio/video containers.
    Convert to a small mono MP3 to avoid container/format rejections (e.g., TS mislabeled as MP4).
    """
    if not src.exists():
        raise FileNotFoundError(f"Source not found for whisper: {src}")
    resolved_ffmpeg = _resolve_ffmpeg()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp_path = Path(tmp.name)
    tmp.close()
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        str(tmp_path),
    ]
    subprocess.run(cmd, check=True)
    new_size_mb = tmp_path.stat().st_size / (1024 * 1024)
    if new_size_mb > size_limit_mb:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Converted audio still too large for whisper ({new_size_mb:.1f}MB)")
    return tmp_path


def _prepare_audio_chunks_for_whisper(
    src: Path, chunk_seconds: int = 1500, size_limit_mb: int = 23
) -> List[Tuple[Path, float]]:
    """
    Convert and split audio into Whisper-sized chunks.
    Returns list of (chunk_path, offset_seconds).
    """
    if not src.exists():
        raise FileNotFoundError(f"Source not found for whisper: {src}")
    resolved_ffmpeg = _resolve_ffmpeg()
    tmp_dir = Path(tempfile.mkdtemp(prefix="whisper_chunks_"))
    pattern = str(tmp_dir / "chunk_%03d.mp3")
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        pattern,
    ]
    subprocess.run(cmd, check=True)
    chunks = sorted(tmp_dir.glob("chunk_*.mp3"))
    if not chunks:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("Failed to create audio chunks for whisper.")
    max_size_mb = max((p.stat().st_size / (1024 * 1024) for p in chunks), default=0.0)
    if max_size_mb > size_limit_mb:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Audio chunk too large for whisper ({max_size_mb:.1f}MB)")
    return [(chunk, idx * float(chunk_seconds)) for idx, chunk in enumerate(chunks)]


def _split_segment_by_word_tags(seg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Given a Whisper segment with word timings, split into logical segments
    where each chunk has a homogeneous tag from classify_words_with_llm.
    If tagging fails or no words, return [seg] unchanged.
    """
    def _lang_from_tag(tag: str) -> str:
        if tag == "arabic_prayer_or_quran":
            return "ar"
        if tag == "mixed":
            return "mixed"
        return "tr"

    words = seg.get("words") or []
    if not isinstance(words, list) or not words:
        return [seg]
    word_texts = []
    word_spans = []
    for w in words:
        w_text = w.get("word") if isinstance(w, dict) else None
        if not w_text:
            continue
        try:
            w_start = float(w.get("start", seg.get("start", 0.0)))
            w_end = float(w.get("end", w_start))
        except Exception:
            continue
        word_texts.append(str(w_text).strip())
        word_spans.append((w_start, w_end))
    # Fallback: if Whisper didn't return word timings, synthesize from text.
    if (not word_texts or len(word_texts) != len(word_spans)) and seg.get("text"):
        word_texts = []
        word_spans = []
        tokens = str(seg.get("text") or "").strip().split()
        try:
            s0 = float(seg.get("start", 0.0) or 0.0)
            e0 = float(seg.get("end", s0) or s0)
            dur = max(e0 - s0, 0.0)
        except Exception:
            s0, e0, dur = 0.0, 0.0, 0.0
        step = dur / len(tokens) if tokens else 0.0
        for idx, tok in enumerate(tokens):
            w_start = s0 + idx * step
            w_end = w_start + step if step > 0 else w_start
            word_texts.append(tok)
            word_spans.append((w_start, w_end))
    if not word_texts or len(word_texts) != len(word_spans):
        return [seg]

    segment_text = seg.get("text") or seg.get("tr_text") or ""
    tags = classify_words_with_llm(word_texts, segment_text)
    if not isinstance(tags, list) or len(tags) != len(word_texts):
        seg_copy = dict(seg)
        seg_copy["word_tags"] = ["turkish_speech"] * len(word_texts)
        if not seg_copy.get("words"):
            seg_copy["words"] = [
                {"word": wt, "start": ws, "end": we}
                for wt, (ws, we) in zip(word_texts, word_spans)
            ]
        return [seg_copy]

    # --- Heuristic smoothing: no dictionary, just patterns & context ---
    def has_arabic_chars(w: str) -> bool:
        return any("\u0600" <= ch <= "\u06FF" for ch in w)

    def has_turkish_chars(w: str) -> bool:
        return any(ch in "çğıöşüÇĞİÖŞÜ" for ch in w)

    # 0) Script rule: if a token has Arabic script, force Arabic
    for i, w in enumerate(word_texts):
        if has_arabic_chars(w):
            tags[i] = "arabic_prayer_or_quran"

    # 1) Split into sentence-like groups by punctuation
    sentence_groups: List[List[int]] = []
    current_group: List[int] = []
    punct_chars = [".", ",", "?", "!", ";", ":", "…"]

    for idx, w in enumerate(word_texts):
        current_group.append(idx)
        if any(ch in w for ch in punct_chars):
            sentence_groups.append(current_group)
            current_group = []
    if current_group:
        sentence_groups.append(current_group)

    smoothed = tags[:]

    for group in sentence_groups:
        if not group:
            continue
        group_tags = [tags[i] for i in group]
        total = len(group_tags)
        ar_indices = [i for i in group if tags[i] == "arabic_prayer_or_quran"]
        ar_count = len(ar_indices)
        if total == 0:
            continue

        ar_ratio = ar_count / total

        # 2a) Single Arabic in an otherwise Turkish sentence (no Arabic script) -> demote
        if ar_count == 1:
            gi = ar_indices[0]
            w = word_texts[gi]
            if not has_arabic_chars(w):
                smoothed[gi] = "turkish_speech"
            continue

        # 2b) Small phrase with high Arabic ratio -> promote all to Arabic
        if 2 <= ar_count <= total and total <= 7 and ar_ratio >= 0.4:
            for gi in group:
                smoothed[gi] = "arabic_prayer_or_quran"
            continue

        # 2c) No Arabic tags but the phrase looks phonetic-Arabic (short block, no Turkish diacritics)
        if ar_count == 0 and total <= 6:
            has_turkish = any(has_turkish_chars(word_texts[i]) for i in group)
            if not has_turkish:
                for gi in group:
                    smoothed[gi] = "arabic_prayer_or_quran"

    # 3) Collapse runs of the same tag to produce contiguous segments
    segments: List[Dict[str, Any]] = []
    if not smoothed:
        return [seg]
    curr_tag = smoothed[0]
    curr_words = [word_texts[0]]
    curr_tags = [smoothed[0]]
    curr_start, curr_end = word_spans[0]
    for i in range(1, len(smoothed)):
        tag = smoothed[i]
        wtxt = word_texts[i]
        wstart, wend = word_spans[i]
        if tag == curr_tag:
            curr_words.append(wtxt)
            curr_tags.append(tag)
            curr_end = wend
        else:
            segments.append(
                {
                    "start": curr_start,
                    "end": curr_end,
                    "duration": max(curr_end - curr_start, 0.0),
                    "text": " ".join(curr_words).strip(),
                    "label": curr_tag,
                    "word_tags": curr_tags[:],
                    "words": [
                        {"word": wt, "start": ws, "end": we}
                        for wt, (ws, we) in zip(curr_words, [word_spans[j] for j in range(i - len(curr_words), i)])
                    ],
                }
            )
            curr_tag = tag
            curr_words = [wtxt]
            curr_tags = [tag]
            curr_start, curr_end = wstart, wend
    segments.append(
        {
            "start": curr_start,
            "end": curr_end,
            "duration": max(curr_end - curr_start, 0.0),
            "text": " ".join(curr_words).strip(),
            "label": curr_tag,
            "word_tags": curr_tags[:],
            "words": [
                {"word": wt, "start": ws, "end": we}
                for wt, (ws, we) in zip(curr_words, word_spans[-len(curr_words):])
            ],
        }
    )
    # Also attach full-length word_tags if no split happened
    if len(segments) == 1 and len(curr_words) == len(word_texts):
        segments[0]["word_tags"] = smoothed
    return segments


def classify_segments_with_llm(segments: List[Dict[str, Any]]) -> List[str]:
    """
    Classify segments as Turkish speech vs Arabic prayer/Quran.
    """
    if not segments:
        return []
    if not _openai_client:
        return ["turkish_speech"] * len(segments)

    prompt = {
        "role": "system",
        "content": (
            "You will classify Turkish lecture segments as either Turkish speech or Arabic/Quran recitation.\n"
            "Return a list of labels, same length as input, using only these labels:\n"
            "- turkish_speech\n"
            "- arabic_prayer_or_quran\n"
            "- other\n\n"
            "Be conservative: only use arabic_prayer_or_quran when the text clearly indicates Quran recitation or Arabic prayer words."
        ),
    }

    user_segments = []
    for seg in segments:
        txt = seg.get("text") or seg.get("tr_text") or seg.get("ar_text") or ""
        user_segments.append({"start": seg.get("start"), "text": txt})
    resp = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[prompt, {"role": "user", "content": json.dumps(user_segments)}],
        response_format={"type": "json_object"},
    )
    try:
        raw = resp.choices[0].message.content if resp.choices else ""
        data = json.loads(raw or "{}")
        labels = data.get("labels") or data.get("tags") or data.get("result") or []
        out = []
        for lbl in labels:
            if lbl in {"turkish_speech", "arabic_prayer_or_quran", "other"}:
                out.append(lbl)
            else:
                out.append("turkish_speech")
        if len(out) != len(segments):
            out = ["turkish_speech"] * len(segments)
        return out
    except Exception:
        return ["turkish_speech"] * len(segments)


def classify_words_with_llm(words: List[str], segment_text: str = "", batch_size: int = 80) -> List[str]:
    """
    Tag each word as Turkish vs Arabic prayer/Quran using an LLM classifier.
    """
    if not words:
        return []
    if not _openai_client:
        return ["turkish_speech"] * len(words)

    default_tags = ["turkish_speech"] * len(words)

    def looks_turkish_charwise(word: str) -> bool:
        turkish_chars = "çğıöşüÇĞİÖŞÜ"
        if any(ch in turkish_chars for ch in word):
            return True
        lower = word.lower().strip(".,!?;:\"'()[]")
        short_turkish = {"ve", "mi", "de", "da", "ki", "bu", "şu", "o", "ben", "sen", "biz", "siz"}
        if lower in short_turkish:
            return True
        return False

    def postprocess_labels(words_list: List[str], labels_list: List[str]) -> List[str]:
        """
        If LLM marked everything as turkish_speech but there is a run of 3+ long,
        non-Turkish-looking tokens, mark that block as arabic_prayer_or_quran.
        """
        n = len(words_list)
        if len(labels_list) != n:
            return labels_list
        labels_mut = list(labels_list)
        i = 0
        while i < n:
            if labels_mut[i] != "turkish_speech":
                i += 1
                continue
            j = i
            block_indices = []
            while j < n and labels_mut[j] == "turkish_speech":
                w = str(words_list[j] or "")
                if (not looks_turkish_charwise(w)) and len(w) >= 6:
                    block_indices.append(j)
                    j += 1
                else:
                    break
            if len(block_indices) >= 3:
                for k in block_indices:
                    labels_mut[k] = "arabic_prayer_or_quran"
                i = block_indices[-1] + 1
            else:
                i += 1
        return labels_mut

    def batch(iterable, size):
        for i in range(0, len(iterable), size):
            yield i, iterable[i:i + size]

    batch_default = ["turkish_speech"] * batch_size
    all_tags: List[str] = []
    for i, batch_words in batch(words, batch_size):
        prompt = {
            "role": "system",
            "content": (
                "You are a word-level language tagger for subtitles of Turkish religious lectures.\n"
                "\n"
                "Input:\n"
                "- JSON with two fields:\n"
                "  - 'segment_text': full transcript text of the segment (usually Turkish, sometimes contains Arabic Quran or duaa).\n"
                "  - 'words': array of token strings in the same order as in 'segment_text'.\n"
                "\n"
                "Your task:\n"
                "- For EACH word in 'words', output ONE label indicating what kind of content it is.\n"
                "- The output MUST be a JSON object with a single key 'labels', whose value is an array of strings.\n"
                "- The length of 'labels' MUST be exactly the same as the length of 'words'.\n"
                "\n"
                "Allowed labels:\n"
                "- 'turkish_speech'           : normal spoken Turkish words or Turkish sentences.\n"
                "- 'arabic_prayer_or_quran'   : Quran recitation or Arabic duaa words, even if written in Latin letters.\n"
                "- 'mixed'                    : a single token that clearly mixes Arabic and Turkish in one word.\n"
                "- 'other'                    : non speech tokens like sound effects, music markers, emojis, noise, etc.\n"
                "\n"
                "GENERAL PRINCIPLES:\n"
                "- Use 'turkish_speech' for normal Turkish sentences and common religious vocabulary used as Turkish speech.\n"
                "- Examples that are usually 'turkish_speech' when inside a Turkish sentence:\n"
                "  Allah, Rabb, peygamber, cennet, cehennem, namaz, dua, sabır, rahmet, ihlas, takva, imtihan, vb.\n"
                "- Never mark an entire clearly Turkish sentence as 'arabic_prayer_or_quran' just because it contains 'Allah'.\n"
                "\n"
                "EVERYDAY TURKISH EXPRESSIONS WITH ALLAH:\n"
                "- Short everyday phrases like:\n"
                "  'Allah esirgesin', 'Allah razı olsun', 'Allah yardımcımız olsun'\n"
                "  should be labeled word by word as 'turkish_speech' when used as normal Turkish speech.\n"
                "\n"
                "INVOCATIONS COMMONLY USED IN TURKISH:\n"
                "- Some Arabic-origin invocations are common in Turkish and may be spoken as part of normal Turkish speech.\n"
                "- When such an invocation appears once and the speaker immediately continues in Turkish,\n"
                "  you may still label that short phrase as 'turkish_speech' word by word so it stays in Turkish Latin script.\n"
                "- Only when a phrase is clearly part of a longer continuous Quran recitation or fully Arabic duaa block\n"
                "  together with several other Arabic-looking words, you should label those words as 'arabic_prayer_or_quran'.\n"
                "\n"
                "WHEN TO USE 'arabic_prayer_or_quran':\n"
                "- Use 'arabic_prayer_or_quran' when a sequence of several consecutive words:\n"
                "  * does not look like normal Turkish morphology,\n"
                "  * would not be grammatical Turkish if read as Turkish,\n"
                "  * sounds like flowing Arabic recitation or duaa when read together.\n"
                "- In such clearly Arabic-looking blocks, each token in that block should be 'arabic_prayer_or_quran'.\n"
                "- Turkish sentences before or after that block MUST stay 'turkish_speech'.\n"
                "\n"
                "MIXED CASES:\n"
                "- A Turkish sentence that quotes a short Arabic phrase should be split logically:\n"
                "  Arabic block inside the quote => 'arabic_prayer_or_quran'\n"
                "  Turkish words before or after => 'turkish_speech'.\n"
                "\n"
                "WHEN TO USE 'mixed':\n"
                "- Use 'mixed' only if a single token clearly combines Arabic and Turkish in one word.\n"
                "- If you are not sure, prefer 'turkish_speech' over 'mixed'.\n"
                "\n"
                "WHEN TO USE 'other':\n"
                "- Use 'other' for tokens that are not normal words in Turkish or Arabic speech\n"
                "  (for example: [music], [applause], pure emojis, raw URLs, editor notes).\n"
                "\n"
                "IMPORTANT CONSTRAINTS:\n"
                "- Do NOT translate, rewrite, or invent new words or sentences.\n"
                "- You only assign labels to the given 'words' array.\n"
                "- Use the surrounding 'segment_text' as context to decide if a token is part of Turkish speech\n"
                "  or part of an Arabic Quran or duaa phrase.\n"
                "- If three or more consecutive tokens are not normal Turkish words and together they form\n"
                "  a phrase that clearly does not follow Turkish grammar, you should strongly prefer\n"
                "  labeling that whole block as 'arabic_prayer_or_quran' instead of 'turkish_speech'.\n"
                "\n"
                "OUTPUT FORMAT:\n"
                "- Return ONLY a JSON object like:\n"
                "  {\"labels\": [\"turkish_speech\", \"turkish_speech\", \"arabic_prayer_or_quran\", ...]}\n"
                "  with no extra commentary.\n"
            ),
        }
        user_payload = {
            "segment_text": segment_text,
            "words": batch_words,
        }
        try:
            resp = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    prompt,
                    {"role": "user", "content": json.dumps(user_payload)},
                ],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content if resp.choices else ""
            data = json.loads(raw or "{}")
            tags = data.get("labels") or data.get("tags") or data.get("result") or []
            cleaned = []
            for t in tags:
                t_str = str(t or "").strip()
                if t_str in {"turkish_speech", "arabic_prayer_or_quran", "mixed", "other"}:
                    cleaned.append(t_str)
                else:
                    cleaned.append("turkish_speech")
            all_tags.extend(cleaned)
        except Exception as e:
            try:
                current_app.logger.warning("LLM word classify batch failed (idx %s-%s): %s", i, i + len(batch_words), e)
            except Exception:
                pass
            all_tags.extend(batch_default)

    if len(all_tags) != len(words):
        try:
            current_app.logger.warning("[WORD_TAG] output length mismatch %s vs %s, using defaults", len(all_tags), len(words))
        except Exception:
            pass
        return default_tags

    all_tags = postprocess_labels(words, all_tags)
    return all_tags


def _normalize_segments_for_use(raw_segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        text = seg.get("text") or ""
        tr_text = seg.get("tr_text") or text
        ar_text = seg.get("ar_text")
        label = seg.get("label")
        try:
            start = float(seg.get("start", 0.0))
        except Exception:
            start = 0.0
        end_val = seg.get("end")
        try:
            end = float(end_val) if end_val is not None else None
        except Exception:
            end = None
        dur_val = seg.get("duration")
        try:
            duration = float(dur_val) if dur_val is not None else None
        except Exception:
            duration = None
        if end is None and duration is not None:
            end = start + max(duration, 0.0)
        if duration is None and end is not None:
            duration = max(end - start, 0.0)
        lang = seg.get("lang") or seg.get("language")
        if label == "arabic_prayer_or_quran":
            lang = "ar"
        if not lang:
            lang = infer_lang_from_text(ar_text or tr_text or text)
        # If we already have an Arabic script text, trust that to mark Arabic.
        if ar_text:
            lang = "ar"
        normalized.append(
            {
                "start": start,
                "end": end if end is not None else start + max(duration or 0.0, 0.0),
                "duration": duration if duration is not None else 0.0,
                "text": tr_text or text or ar_text or "",
                "tr_text": tr_text or text or ar_text or "",
                "ar_text": ar_text,
                "label": label,
                "lang": lang,
                "words": seg.get("words"),
                "word_tags": seg.get("word_tags"),
            }
        )
    return normalized


def _refine_arabic_segments_with_whisper(segments: List[Dict[str, Any]], video_path: Path) -> List[Dict[str, Any]]:
    """
    For segments tagged as Arabic, re-run Whisper with language='ar' on that time window to get Arabic script.
    """
    if not _openai_client:
        return segments
    updated = []
    for seg in segments:
        seg_copy = dict(seg)
        tr_txt = seg_copy.get("tr_text") or seg_copy.get("text") or ""
        lang_val = (seg_copy.get("lang") or "").lower()
        needs_ar_refine = lang_val == "ar" or bool(seg_copy.get("force_ar"))
        if not needs_ar_refine:
            updated.append(seg_copy)
            continue
        try:
            start = float(seg_copy.get("start", 0.0))
            end = seg_copy.get("end")
            dur = seg_copy.get("duration")
            if end is None:
                end = start + max(float(dur or 0.0), 0.0)
            else:
                end = float(end)
            if end <= start:
                raise ValueError("invalid segment timing")
        except Exception:
            updated.append(seg_copy)
            continue

        snippet_path = None
        try:
            snippet_path = _extract_audio_segment(video_path, start, end)
            with snippet_path.open("rb") as f:
                resp = _openai_client.audio.transcriptions.create(
                    model=WHISPER_MODEL,
                    file=f,
                    response_format="text",
                    language="ar",
                )
            refined_txt = ""
            if hasattr(resp, "text"):
                refined_txt = resp.text or ""
            else:
                refined_txt = str(resp or "")
            refined_txt = str(refined_txt or "").strip()
            if refined_txt:
                seg_copy["ar_text"] = refined_txt
                if not seg_copy.get("text"):
                    seg_copy["text"] = refined_txt
                seg_copy["lang"] = "ar"
        except Exception as e:
            try:
                current_app.logger.warning("Arabic refinement failed for segment %.2f-%.2f: %s", start, end, e)
            except Exception:
                pass
        finally:
            if snippet_path and snippet_path.exists():
                try:
                    snippet_path.unlink()
                except Exception:
                    pass
        updated.append(seg_copy)
    return updated


def _fetch_transcript(conn, video_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    cols = _ensure_transcript_schema(conn)
    has_whisper_col = "whisper_segments_json" in cols
    select_sql = (
        "SELECT full_text, segments_json, whisper_segments_json FROM youtube_transcripts WHERE video_id = ?"
        if has_whisper_col
        else "SELECT full_text, segments_json, NULL as whisper_segments_json FROM youtube_transcripts WHERE video_id = ?"
    )
    row = conn.execute(select_sql, [video_id]).fetchone()
    if not row:
        return "", []
    full_text = row[0] or ""
    segments_raw_json = row[2] if has_whisper_col and row[2] else row[1]
    segments: List[Dict[str, Any]] = []
    if segments_raw_json:
        try:
            parsed = json.loads(segments_raw_json)
            if isinstance(parsed, list):
                segments = parsed
        except Exception:
            segments = []
    return full_text, _normalize_segments_for_use(segments)


def _whisper_response_to_segments(resp: Any) -> Tuple[str, List[Dict[str, Any]]]:
    # Preserve Whisper's full text as-is
    try:
        full_text_raw = getattr(resp, "text", None)
    except Exception:
        full_text_raw = None
    full_text = str(full_text_raw or "").strip()

    def _word_to_dict(w):
        if isinstance(w, dict):
            return {
                "word": w.get("word"),
                "start": w.get("start"),
                "end": w.get("end"),
            }
        try:
            return {
                "word": getattr(w, "word", None),
                "start": getattr(w, "start", None),
                "end": getattr(w, "end", None),
            }
        except Exception:
            return {"word": None, "start": None, "end": None}

    def _segment_to_dict(seg: Any) -> Dict[str, Any]:
        words_raw = _seg_val(seg, "words") or []
        words_list = []
        if isinstance(words_raw, list):
            for w in words_raw:
                wd = _word_to_dict(w)
                if wd:
                    words_list.append(wd)
        start_val = _seg_val(seg, "start", 0.0) or 0.0
        end_val = _seg_val(seg, "end")
        dur_val = _seg_val(seg, "duration")
        text_val = _seg_val(seg, "text") or ""
        # Synthesize word timings if missing but text exists
        if (not words_list) and text_val:
            tokens = [t for t in text_val.strip().split() if t]
            try:
                s0 = float(start_val)
            except Exception:
                s0 = 0.0
            try:
                e0 = float(end_val) if end_val is not None else None
            except Exception:
                e0 = None
            try:
                d0 = float(dur_val) if dur_val is not None else None
            except Exception:
                d0 = None
            if e0 is None and d0 is not None:
                e0 = s0 + max(d0, 0.0)
            if d0 is None and e0 is not None:
                d0 = max(e0 - s0, 0.0)
            if d0 is None:
                d0 = 0.0
            step = (d0 / len(tokens)) if tokens else 0.0
            cur = s0
            for tok in tokens:
                w_start = cur
                w_end = cur + step if step > 0 else cur
                words_list.append({"word": tok, "start": w_start, "end": w_end})
                cur = w_end
        return {
            "start": start_val,
            "end": end_val,
            "duration": dur_val,
            "text": text_val,
            "words": words_list,
        }

    segments = []
    try:
        raw_segments = getattr(resp, "segments", None) or []
    except Exception:
        raw_segments = []
    for s in raw_segments:
        try:
            segments.append(_segment_to_dict(s))
        except Exception:
            continue
    return full_text, segments


def _offset_segments(segments: List[Dict[str, Any]], offset: float) -> None:
    if not offset:
        return
    for seg in segments:
        for key in ("start", "end"):
            if seg.get(key) is not None:
                try:
                    seg[key] = float(seg[key]) + offset
                except Exception:
                    pass
        words = seg.get("words")
        if isinstance(words, list):
            for w in words:
                if not isinstance(w, dict):
                    continue
                for key in ("start", "end"):
                    if w.get(key) is not None:
                        try:
                            w[key] = float(w[key]) + offset
                        except Exception:
                            pass


def _transcribe_with_whisper(
    video_path: Path,
    progress_cb: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    def _emit(stage: str, message: str, **extra: Any) -> None:
        if not progress_cb:
            return
        try:
            progress_cb(stage, message, extra)
        except Exception:
            pass

    _emit("start", "Transcription job started.")
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY is missing; cannot run Whisper.")
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found at {video_path}")

    _emit("language_probe", "Detecting primary audio language.")
    transcription_mode = _detect_primary_audio_language(video_path)
    if transcription_mode == "en":
        _emit("language_mode", "Language detected: English. Running English flow.")
    elif transcription_mode == "tr":
        _emit("language_mode", "Language detected: Turkish. Running Turkish flow.")
    else:
        _emit("language_mode", "Language uncertain. Running auto mode.")

    audio_jobs: List[Tuple[Path, float]] = []
    cleanup_paths: List[Path] = []
    cleanup_dirs = set()
    audio_ref_path: Path = video_path

    try:
        _emit("prepare_audio", "Preparing audio for Whisper.")
        audio_path = _prepare_audio_for_whisper(video_path)
        audio_jobs = [(audio_path, 0.0)]
        cleanup_paths.append(audio_path)
        audio_ref_path = audio_path
        _emit("prepare_audio_done", "Audio prepared.")
    except RuntimeError as exc:
        if "too large" not in str(exc).lower():
            raise
        _emit("prepare_chunks", "Source is large; splitting into chunks.")
        audio_jobs = _prepare_audio_chunks_for_whisper(video_path)
        cleanup_paths.extend([p for p, _ in audio_jobs])
        for p in cleanup_paths:
            if p.parent.name.startswith("whisper_chunks_"):
                cleanup_dirs.add(p.parent)
        audio_ref_path = video_path
        _emit("prepare_chunks_done", "Chunking complete.", chunk_count=len(audio_jobs))

    full_text_parts: List[str] = []
    segments: List[Dict[str, Any]] = []
    chunk_languages: List[str] = []
    total_chunks = len(audio_jobs)
    _emit("whisper_start", "Sending audio to Whisper.", chunk_count=total_chunks)
    for index, (audio_path, offset) in enumerate(audio_jobs, start=1):
        _emit(
            "whisper_chunk_start",
            f"Transcribing chunk {index}/{total_chunks}.",
            chunk_index=index,
            chunk_count=total_chunks,
        )
        with audio_path.open("rb") as f:
            request_payload: Dict[str, Any] = {
                "model": WHISPER_MODEL,
                "file": f,
                "response_format": "verbose_json",
                "timestamp_granularities": ["segment", "word"],  # ensure segment + word timings
            }
            if transcription_mode in {"en", "tr"}:
                request_payload["language"] = transcription_mode
            resp = _openai_client.audio.transcriptions.create(
                **request_payload,
            )
        chunk_lang = _extract_transcription_language(resp)
        if chunk_lang:
            chunk_languages.append(chunk_lang)
        chunk_text, chunk_segments = _whisper_response_to_segments(resp)
        _offset_segments(chunk_segments, offset)
        if chunk_text:
            full_text_parts.append(chunk_text)
        segments.extend(chunk_segments)
        _emit(
            "whisper_chunk_done",
            f"Chunk {index}/{total_chunks} transcribed.",
            chunk_index=index,
            chunk_count=total_chunks,
            segment_count=len(chunk_segments),
        )

    full_text = " ".join(full_text_parts).strip()

    def _overlap(a, b):
        return a[0] < b[1] and b[0] < a[1]

    def _seg_bounds(seg):
        try:
            s = float(_seg_val(seg, "start", 0.0) or 0.0)
        except Exception:
            s = 0.0
        e_val = _seg_val(seg, "end")
        try:
            e = float(e_val) if e_val is not None else None
        except Exception:
            e = None
        d_val = _seg_val(seg, "duration")
        try:
            d = float(d_val) if d_val is not None else None
        except Exception:
            d = None
        if e is None:
            e = s + max(d or 0.0, 0.0)
        if d is None:
            d = max(e - s, 0.0)
        return s, e, d

    # Convert whisper segments to normal form (defensive against object or dict)
    normalized = []
    for seg in segments:
        try:
            start = float(_seg_val(seg, "start", 0.0) or 0.0)
        except Exception:
            start = 0.0
        dur_val = _seg_val(seg, "duration")
        try:
            duration = float(dur_val) if dur_val is not None else None
        except Exception:
            duration = None
        end_val = _seg_val(seg, "end")
        try:
            end = float(end_val) if end_val is not None else None
        except Exception:
            end = None
        if end is None and duration is not None:
            end = start + max(duration, 0.0)
        if duration is None and end is not None:
            duration = max(end - start, 0.0)
        normalized.append(
            {
                "start": start,
                "end": end,
                "duration": duration,
                "text": _seg_val(seg, "text") or "",
                "words": _seg_val(seg, "words") or [],
            }
        )

    # Keep Whisper transcripts exactly as produced for every language.
    # Downstream subtitle and clip flows expect normalized segments, but they do
    # not require the slower language-classification or Arabic refinement passes.
    en_chunks = sum(1 for lang in chunk_languages if lang == "en")
    looks_english = _looks_english_text(full_text)
    english_mode = (
        transcription_mode == "en"
        or (
            transcription_mode != "tr"
            and ((en_chunks and en_chunks >= max(1, len(chunk_languages) // 2)) or looks_english)
        )
    )
    detected_lang = "en" if english_mode else (transcription_mode if transcription_mode in {"en", "tr"} else "")
    if not detected_lang:
        inferred_lang = infer_lang_from_text(full_text or "")
        detected_lang = "en" if inferred_lang == "en" else "tr"

    _emit("language_passthrough", "Preserving Whisper output.")
    segments: List[Dict[str, Any]] = []
    for seg in normalized:
        seg_text = (seg.get("text") or "").strip()
        segments.append(
            {
                "start": seg.get("start", 0.0),
                "end": seg.get("end"),
                "duration": seg.get("duration"),
                "lang": detected_lang,
                "label": "speech",
                "tr_text": seg_text,
                "ar_text": None,
                "text": seg_text,
                "words": seg.get("words"),
                "word_tags": None,
            }
        )

    _emit("cleanup", "Cleaning temporary files.")
    # Cleanup temp audio if created
    if cleanup_paths:
        for path in cleanup_paths:
            try:
                path.unlink()
            except Exception:
                pass
        for tmp_dir in cleanup_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    _emit("done", "Transcription finished.", segment_count=len(segments))
    return str(full_text), segments
