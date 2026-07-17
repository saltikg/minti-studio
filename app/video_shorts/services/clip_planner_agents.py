import json
import math
from typing import List, Dict, Any, Tuple, Optional

from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.clip_plan_focus_prompts import get_agent_focus_block
from app.video_shorts.services.clip_title import generate_clip_title

OPENAI_PLANNER_TIMEOUT_SECONDS = 45.0
MIN_CLIP_SECONDS = 25.0
MAX_CLIP_SECONDS = 75.0


def merge_segments_into_sentences(segments: List[Dict[str, Any]], max_gap: float = 0.8) -> List[Dict[str, Any]]:
    sentence_blocks: List[Dict[str, Any]] = []
    sorted_segments = sorted(segments or [], key=lambda s: float(s.get("start") or 0.0))

    def _get_text(source: Dict[str, Any]) -> str:
        return (source.get("tr_text") or source.get("text") or source.get("ar_text") or "").strip()

    def _flush(block: Dict[str, Any]) -> None:
        sentence_blocks.append(
            {
                "start": block["start"],
                "end": block["end"],
                "duration": block["end"] - block["start"],
                "text": block["text"].strip(),
            }
        )

    current_block: Optional[Dict[str, Any]] = None
    last_segment_start: Optional[float] = None

    for seg in sorted_segments:
        text = _get_text(seg)
        if not text:
            continue
        try:
            start = float(seg.get("start") or 0.0)
        except Exception:
            start = 0.0
        try:
            end = float(seg.get("end")) if seg.get("end") is not None else None
        except Exception:
            end = None
        if end is None:
            try:
                end = start + float(seg.get("duration") or 0.0)
            except Exception:
                end = start
        if end < start:
            end = start

        def _start_block() -> Dict[str, Any]:
            return {
                "start": start,
                "end": end,
                "text": text,
                "char_count": len(text),
            }

        if current_block is None:
            current_block = _start_block()
        else:
            gap = start - (last_segment_start if last_segment_start is not None else start)
            if gap > max_gap or current_block["char_count"] + len(text) + 1 > 220:
                _flush(current_block)
                current_block = _start_block()
            else:
                current_block["text"] = f"{current_block['text']} {text}"
                current_block["end"] = end
                current_block["char_count"] += len(text) + 1

        last_segment_start = start
        if text and text[-1] in {".", "!", "?"}:
            if current_block:
                _flush(current_block)
                current_block = None
            last_segment_start = None

    if current_block:
        _flush(current_block)
    return sentence_blocks


def build_windows(duration_seconds: float, window_size: float = 240.0, stride: float = 120.0, context_margin: float = 30.0) -> List[Dict[str, Any]]:
    if not duration_seconds or duration_seconds <= 0:
        return []
    windows = []
    start = 0.0
    while start < duration_seconds:
        end = min(start + window_size, duration_seconds)
        win = {
            "start": round(start, 2),
            "end": round(end, 2),
            "context_start": max(0.0, start - context_margin),
            "context_end": min(duration_seconds, end + context_margin),
        }
        windows.append(win)
        if end >= duration_seconds:
            break
        start += stride
    return windows


def extract_segments_for_window(segments: List[Dict[str, Any]], window: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs = []
    ctx_start = window["context_start"]
    ctx_end = window["context_end"]
    for s in segments or []:
        try:
            st = float(s.get("start", 0.0) or 0.0)
            dur = float(s.get("duration", 0.0) or 0.0)
        except Exception:
            continue
        en = st + max(dur, 0.0)
        if en < ctx_start or st > ctx_end:
            continue
        segs.append(s)
    return segs


def normalize_segments_for_agent(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    sentence_segments = merge_segments_into_sentences(segments)
    for seg in sentence_segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = seg.get("start") or 0.0
        end = seg.get("end") if seg.get("end") is not None else start
        duration = seg.get("duration")
        if duration is None:
            duration = max(end - start, 0.0)
        normalized.append(
            {
                "start": start,
                "end": end,
                "duration": duration,
                "text": text,
            }
        )
    return normalized


def _clean_clip_list(raw_clips: List[Dict[str, Any]], duration_seconds: float) -> List[Dict[str, Any]]:
    cleaned = []
    for c in raw_clips or []:
        try:
            start = float(c.get("start"))
            end = float(c.get("end"))
        except Exception:
            continue
        if duration_seconds:
            end = min(end, float(duration_seconds))
        if end <= start:
            continue
        duration = end - start
        cleaned.append(
            {
                "title": c.get("title") or "",
                "start": round(start, 2),
                "end": round(end, 2),
                "excerpt": c.get("excerpt") or "",
            }
        )
    return cleaned


def _clip_duration(candidate: Dict[str, Any]) -> float:
    try:
        return float(candidate.get("end") or 0.0) - float(candidate.get("start") or 0.0)
    except Exception:
        return 0.0


def _clip_text_for_range(segments: List[Dict[str, Any]], start: Any, end: Any, excerpt: str = "") -> str:
    try:
        clip_start = float(start)
        clip_end = float(end)
    except Exception:
        return str(excerpt or "").strip()

    matched: List[str] = []
    for seg in segments or []:
        try:
            seg_start = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        seg_end_val = seg.get("end")
        try:
            seg_end = float(seg_end_val) if seg_end_val is not None else None
        except Exception:
            seg_end = None
        if seg_end is None:
            try:
                seg_end = seg_start + max(float(seg.get("duration") or 0.0), 0.0)
            except Exception:
                seg_end = seg_start
        if seg_end <= clip_start or seg_start >= clip_end:
            continue
        text = str(seg.get("text") or "").strip()
        if text:
            matched.append(text)
    joined = " ".join(matched).strip()
    return joined or str(excerpt or "").strip()


def _call_agent2_fix_clip(window: Dict[str, Any], segments: List[Dict[str, Any]], candidate_clip: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _openai_client:
        return None
    
    system_prompt = (
        "Sen bir 'clip fixer' ajansın.\n"
        "Girdi olarak üç şey alıyorsun:\n"
        "- window: {\"start\": float, \"end\": float}\n"
        "- segments: bu window içindeki zaman sıralı cümle blokları listesi. Her segment:\n"
        "    {\"start\": float, \"end\": float, \"duration\": float, \"text\": str}\n"
        "- base_clip: Agent 1 tarafından önerilen klip. Şu yapıda:\n"
        "    {\"title\": str, \"start\": float, \"end\": float, \"excerpt\": str}\n"
        "\n"
        "Video dili Türkçe.\n"
        "Görevin, base_clip ile aynı fikir çekirdeğini koruyarak onu gerekirse öne ve arkaya doğru genişletip\n"
        "klip süresini en az 25 saniyeye çıkarmaktır.\n"
        "\n"
        "KATI KURALLAR:\n"
        "1) Üreteceğin klip her zaman window.start ile window.end aralığında kalmalıdır.\n"
        "   - Yeni_start >= window.start\n"
        "   - Yeni_end   <= window.end\n"
        "\n"
        "2) Base klip mutlaka yeni aralığın içinde kalmalıdır.\n"
        "   - new_start <= base_clip.start\n"
        "   - new_end   >= base_clip.end\n"
        "\n"
        "3) Start ve end sadece segment sınırlarında olabilir.\n"
        "   - new_start, segments içindeki bir segment.start ile tam aynı olmalı\n"
        "   - new_end   segments içindeki bir segment.end ile tam aynı olmalı\n"
        "   - Aradaki tüm segmentler klibin parçası sayılır ve klipte EN AZ 2 segment olmalıdır.\n"
        "\n"
        "4) Süre kuralı:\n"
        "   - Süre = new_end - new_start\n"
        "   - Süre 25 saniyeden KESİNLİKLE az olmamalıdır.\n"
        "   - Süre 75 saniyeyi KESİNLİKLE geçmemelidir.\n"
        "   - İdeal aralık 40 ile 60 saniye arasıdır.\n"
        "\n"
        "5) Genişletme mantığı:\n"
        "   - Önce base_clip in kapsadığı segment aralığını bul.\n"
        "   - Eğer bu aralığın süresi zaten >= 25 ise, aynı aralığı kullan, sadece excerpt i temizce üret.\n"
        "   - Eğer süre < 25 ise, önce base_clip ten ÖNCE gelen segmentleri tek tek ekleyerek\n"
        "     süreyi artırmaya çalış. Yine yetmezse base_clip ten SONRA gelen segmentleri ekle.\n"
        "   - Genişletirken aynı konu ve fikir bütünlüğünü korumaya dikkat et. Farklı bir konuya sıçrama yapma.\n"
        "\n"
        "6) Eğer window içinde base_clip ile aynı fikri bozmadan ve window sınırlarını aşmadan\n"
        "   25 saniyeye ulaşamıyorsan HİÇ klip üretme.\n"
        "\n"
        "7) Sadece TEK bir klip döndüreceksin. Bu çağrı sadece bir base_clip i düzeltmek içindir.\n"
        "\n"
        "ÇIKTI FORMAT KURALI:\n"
        "Sadece geçerli JSON döndür. Ekstra açıklama, yorum veya metin yazma.\n"
        "{\n"
        "  \"clips\": [\n"
        "    {\"start\": float, \"end\": float, \"excerpt\": str}\n"
        "  ]\n"
        "}\n"
        "\n"
        "start, end: Yukarıdaki kurallara uygun zaman değerleri.\n"
        "excerpt: Seçtiğin segmentlerin içinden 1 ile 3 cümle arası çarpıcı bir alıntı.\n"
        "Eğer uygun bir genişletme yapamıyorsan:\n"
        "{ \"clips\": [] }\n"
        "dönmelisin.\n"
    )

    payload = {
        "window": window,
        "segments": segments,
        "base_clip": candidate_clip,
    }
    try:
        resp = _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            timeout=OPENAI_PLANNER_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
        clips = data.get("clips") or []
    except Exception:
        return None
    if not clips:
        return None
    clip_data = clips[0] or {}
    try:
        start = float(clip_data.get("start"))
        end = float(clip_data.get("end"))
    except Exception:
        return None
    window_start = float(window.get("start") or 0.0)
    window_end_raw = window.get("end")
    window_end = float(window_end_raw) if window_end_raw is not None else None
    start = max(start, window_start)
    if window_end is not None:
        end = min(end, window_end)
    duration = end - start
    if duration < MIN_CLIP_SECONDS or duration > MAX_CLIP_SECONDS:
        return None
    clip_excerpt = clip_data.get("excerpt") or candidate_clip.get("excerpt") or ""
    return {
        "title": generate_clip_title(
            _clip_text_for_range(segments, start, end, excerpt=str(clip_excerpt or "")),
            language_hint="tr",
        ),
        "start": round(start, 2),
        "end": round(end, 2),
        "excerpt": clip_excerpt,
    }


def run_window_agent(
    client,
    model: str,
    window: Dict[str, Any],
    segments: List[Dict[str, Any]],
    transcript_excerpt: str = "",
    plan_focus: str = "",
) -> Tuple[List[Dict[str, Any]], str, str]:
    excerpt_text = transcript_excerpt
    if not excerpt_text:
        excerpt_text = " ".join(s.get("text", "") for s in segments if s.get("text"))
    if excerpt_text:
        if len(excerpt_text) > 4000:
            excerpt_text = excerpt_text[:4000]
        elif len(excerpt_text) < 2000:
            excerpt_text = excerpt_text[:2000]
    payload = {
        "window": window,
        "segments": segments,
        "transcript_excerpt": excerpt_text,
    }
    focus_block = get_agent_focus_block(plan_focus)
    system_prompt = (
        "Sen bir klipleme ajanısın. Video dili Türkçe. Verilen cümle segmentlerinden, "
        "YouTube Shorts için anlamlı mini sohbet klipleri öneriyorsun.\n"
        "\n"
        "Girdi yapısı:\n"
        "- 'segments' listesi zaman sıralı cümle bloklarıdır. Her birinde: start, end, duration (saniye), text alanları vardır.\n"
        "- 'window.start' ve 'window.end' bu ajanın asıl çalıştığı zaman aralığını gösterir. "
        "Seçeceğin tüm klipler bu aralığın içinde kalmalıdır.\n"
        "\n"
        "Aradığın yoğunluk türleri:\n"
        "- tez: Ana fikri taşıyan, hüküm ve çerçeve cümleleri\n"
        "- ikaz: Sarsıcı uyarılar, tehlike vurguları, güçlü ikazlar\n"
        "- vecize: Kısa, duvara asılacak özlü sözler ya da yoğun çıkarımlar\n"
        "- hikaye: Kişi, yer, olay veya hatıra anlatan bölümler\n"
        "- cozum: 'Ne yapmalı?' sorusuna cevap veren, yol haritası çizen bölümler\n"
        "- duygu: Açık duygu içeren, sitem, acı, hayret, öfke veya sevgi taşıyan ifadeler\n"
        f"{focus_block}"
        "\n"
        "EN ÖNEMLİ KURALLAR:\n"
        "1) Klipler TEK CÜMLE olmayacak, küçük bir sohbet/paragraf bloğu olacak.\n"
        "   Her klip birden fazla segment içermeli (genelde 2 ile 6 segment arası).\n"
        "2) Kesin kural: Bir klipte (end - start) 25 saniyeden KESİNLİKLE az olmayacak.\n"
        "   - Eğer güçlü bir cümle kısa sürüyorsa, aynı window içindeki ÖNCEKİ ve SONRAKİ segmentleri de ekleyerek\n"
        "     süreyi en az 25 saniyeye çıkaracaksın.\n"
        "   - 25 saniyenin altında kalan klip üretmeyeceksin.\n"
        "   - Bir klip 75 saniyeyi KESİNLİKLE geçmeyecek. 75 saniyenin üstüne çıkan klip üretmeyeceksin.\n"
        "\n"
        "Süre kuralları:\n"
        "- İdeal klip süresi 40 ile 60 saniye arasındadır.\n"
        "- Süreyi hesaplarken seçtiğin segmentleri kullan:\n"
        "  * Klipte dahil ettiğin ilk segmentin start değeri klibin start değeri olsun.\n"
        "  * Klipte dahil ettiğin son segmentin end değeri klibin end değeri olsun.\n"
        "  * Klip süresi = end - start.\n"
        "- Sert üst sınır 75 saniyedir. 75 saniyeyi aşan klip geçersizdir.\n"
        "  Her klibin süresi doğal akıştan gelsin ama bu sınırı asla aşmasın.\n"
        "\n"
        "Klip üretim adımları:\n"
        "1) Window içindeki segmentlerde tez, ikaz, vecize, hikaye, cozum veya duygu içeren güçlü bir çekirdek cümle bul.\n"
        "2) Bu çekirdek etrafında bağlam oluştur:\n"
        "   - Önceki segmentlerden, konuyu hazırlayan cümleleri ekle.\n"
        "   - Sonraki segmentlerden, anlamı tamamlayan cümleleri ekle.\n"
        "3) Bu genişletme ile klip süresini en az 25 saniyeye çıkar:\n"
        "   - Gerekirse 3-4 segmenti birleştir, doğal bir mini sohbet çıkana kadar segment eklemeye devam et.\n"
        "4) Klip cümle ortasında başlamasın. Başlangıç, doğal bir cümlenin başı olsun.\n"
        "   Eğer ilk segment 've', 'ama', 'fakat' gibi bağlaçlarla başlıyorsa, ondan önceki segmenti de mutlaka ekle.\n"
        "   Klip cümle ortasında bitmesin. Son segment tamamlanmamış bir düşünce, virgül veya açık bağlaçla bitiyorsa,\n"
        "   SADECE o düşünceyi tamamlayan bir sonraki cümleyi ekle; yeni bir konu açacak şekilde art arda ekleme yapma.\n"
        "5) Aynı mesajı tekrar eden, neredeyse aynı çekirdeği taşıyan klipleri çoğaltma.\n"
        "   Benzer yoğunlukları tek klipte topla.\n"
        "6) Bu window içinde en fazla 2 klip üretebilirsin.\n"
        "   Çok sayıda kısa klip yerine az sayıda dolu ve derin klip tercih et.\n"
        "\n"
        "KONTROL ADIMI (SÜRE):\n"
        "Her klibi döndürmeden önce şu kontrolü zihninde yap:\n"
        "- Klipte kullandığın ilk segmentin start değeri = s\n"
        "- Klipte kullandığın son segmentin end değeri = e\n"
        "- Eğer (e - s) < 25 ise bu klibi KULLANMA. Bu durumda:\n"
        "  * Önce komşu segmentler eklenerek süreyi 25 saniyenin üstüne çıkarmaya çalış.\n"
        "  * Hala 25 saniyenin altında kalıyorsa, o klibi tamamen iptal et.\n"
        "- Eğer (e - s) > 75 ise bu klibi KULLANMA.\n"
        "\n"
        "KONTROL ADIMI (ÇAKIŞMA):\n"
        "- Aynı 30 saniyelik zaman aralığı içinde neredeyse aynı segmentleri kullanan iki farklı klip oluşturma.\n"
        "- Eğer iki aday klip büyük ölçüde aynı segmentleri veya aynı ana fikri paylaşıyorsa,\n"
        "  yalnızca en güçlü ve en dolu olan klibi bırak, diğerini tamamen iptal et.\n"
        "\n"
        "Çıktı formatı:\n"
        "Sadece geçerli JSON döndür. Şu yapıda üret:\n"
        "{\n"
        '  \"clips\": [\n'
        '    {\"start\": float, \"end\": float, \"excerpt\": str},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "\n"
        "excerpt: Klip içinden 1-3 cümlelik çarpıcı bir alıntı olsun. "
        "Bu alıntı da tek başına anlaşılır ve izleyiciye ne göreceğini hissettiren türden olmalıdır.\n"
    )
 

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
        timeout=OPENAI_PLANNER_TIMEOUT_SECONDS,
    )
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
        clips = _clean_clip_list(data.get("clips"), window["context_end"])
    except Exception:
        clips = []
    for clip in clips:
        clip["title"] = generate_clip_title(
            _clip_text_for_range(
                segments,
                clip.get("start"),
                clip.get("end"),
                excerpt=str(clip.get("excerpt") or ""),
            ),
            language_hint="tr",
        )
    return clips, raw, excerpt_text


def _dedupe_candidates_by_time(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sorted_cands = sorted(candidates, key=lambda x: x.get("start", 0))
    deduped = []
    for c in sorted_cands:
        if not deduped:
            deduped.append(c)
            continue
        last = deduped[-1]
        start_diff = abs((c.get("start") or 0) - (last.get("start") or 0))
        end_diff = abs((c.get("end") or 0) - (last.get("end") or 0))
        if start_diff < 1.0 and end_diff < 1.0:
            continue
        deduped.append(c)
    return deduped


def _target_clip_count_for_duration(duration_seconds: float) -> int:
    if not duration_seconds or duration_seconds <= 0:
        return 3
    return max(3, min(12, int(math.ceil(float(duration_seconds) / 150.0))))


def _find_sentence_segment_for_time(
    sentence_segments: List[Dict[str, Any]],
    timestamp: float,
    *,
    prefer: str,
) -> Optional[Dict[str, Any]]:
    if not sentence_segments:
        return None
    for seg in sentence_segments:
        try:
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or start)
        except Exception:
            continue
        if prefer == "start" and start <= timestamp < end:
            return seg
        if prefer == "end" and start < timestamp <= end:
            return seg

    best_seg: Optional[Dict[str, Any]] = None
    best_distance: Optional[float] = None
    for seg in sentence_segments:
        try:
            boundary = float(seg.get("start") if prefer == "start" else seg.get("end"))
        except Exception:
            continue
        distance = abs(boundary - timestamp)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_seg = seg
    return best_seg


def _snap_clip_to_sentence_boundaries(candidate: Dict[str, Any], sentence_segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    snapped = dict(candidate or {})
    try:
        raw_start = float(candidate.get("start"))
        raw_end = float(candidate.get("end"))
    except Exception:
        return snapped
    if raw_end <= raw_start or not sentence_segments:
        return snapped

    start_seg = _find_sentence_segment_for_time(sentence_segments, raw_start, prefer="start")
    end_seg = _find_sentence_segment_for_time(sentence_segments, raw_end, prefer="end")
    if not start_seg or not end_seg:
        return snapped

    try:
        start = float(start_seg.get("start") or raw_start)
    except Exception:
        start = raw_start
    try:
        end = float(end_seg.get("end") or raw_end)
    except Exception:
        end = raw_end

    if end <= start:
        try:
            start_idx = sentence_segments.index(start_seg)
            end_idx = sentence_segments.index(end_seg)
        except ValueError:
            start_idx = -1
            end_idx = -1
        if end_idx < start_idx and start_idx >= 0:
            end_seg = sentence_segments[start_idx]
            try:
                end = float(end_seg.get("end") or raw_end)
            except Exception:
                end = raw_end
        if end <= start:
            return snapped

    snapped["start"] = round(start, 2)
    snapped["end"] = round(end, 2)
    return snapped


def _trim_candidate_to_duration_limit(
    candidate: Dict[str, Any],
    sentence_segments: List[Dict[str, Any]],
    *,
    min_seconds: float = MIN_CLIP_SECONDS,
    max_seconds: float = MAX_CLIP_SECONDS,
) -> Optional[Dict[str, Any]]:
    trimmed = dict(candidate or {})
    duration = _clip_duration(trimmed)
    if duration <= 0:
        return None
    if duration < min_seconds:
        return None
    if duration <= max_seconds:
        return trimmed
    if not sentence_segments:
        return None

    try:
        raw_start = float(trimmed.get("start"))
        raw_end = float(trimmed.get("end"))
    except Exception:
        return None

    start_seg = _find_sentence_segment_for_time(sentence_segments, raw_start, prefer="start")
    end_seg = _find_sentence_segment_for_time(sentence_segments, raw_end, prefer="end")
    if not start_seg or not end_seg:
        return None
    try:
        start_idx = sentence_segments.index(start_seg)
        end_idx = sentence_segments.index(end_seg)
    except ValueError:
        return None
    if end_idx < start_idx:
        return None

    best_candidate: Optional[Dict[str, Any]] = None
    best_duration = -1.0

    # Prefer trimming from the end while keeping the original start/core.
    for candidate_end_idx in range(end_idx, start_idx - 1, -1):
        start = float(sentence_segments[start_idx].get("start") or raw_start)
        end = float(sentence_segments[candidate_end_idx].get("end") or raw_end)
        candidate_duration = end - start
        if candidate_duration < min_seconds:
            break
        if candidate_duration <= max_seconds and candidate_duration > best_duration:
            best_duration = candidate_duration
            best_candidate = dict(trimmed, start=round(start, 2), end=round(end, 2))
            break

    # If the original start makes every option too long, slide the start forward.
    if best_candidate is None:
        for candidate_start_idx in range(start_idx + 1, end_idx + 1):
            start = float(sentence_segments[candidate_start_idx].get("start") or raw_start)
            end = float(sentence_segments[end_idx].get("end") or raw_end)
            candidate_duration = end - start
            if candidate_duration < min_seconds:
                continue
            if candidate_duration <= max_seconds and candidate_duration > best_duration:
                best_duration = candidate_duration
                best_candidate = dict(trimmed, start=round(start, 2), end=round(end, 2))
                break

    return best_candidate


def _overlap_ratio(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    try:
        a_start = float(a.get("start") or 0.0)
        a_end = float(a.get("end") or a_start)
        b_start = float(b.get("start") or 0.0)
        b_end = float(b.get("end") or b_start)
    except Exception:
        return 0.0
    overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    if overlap <= 0:
        return 0.0
    shorter = min(max(a_end - a_start, 0.0), max(b_end - b_start, 0.0))
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def _prune_overlapping_selected_clips(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(candidates or [], key=lambda item: float(item.get("start") or 0.0))
    pruned: List[Dict[str, Any]] = []
    for candidate in ordered:
        if not pruned:
            pruned.append(candidate)
            continue
        last = pruned[-1]
        if _overlap_ratio(last, candidate) >= 0.3:
            try:
                last_duration = float(last.get("end") or 0.0) - float(last.get("start") or 0.0)
            except Exception:
                last_duration = 0.0
            try:
                cand_duration = float(candidate.get("end") or 0.0) - float(candidate.get("start") or 0.0)
            except Exception:
                cand_duration = 0.0
            if cand_duration > last_duration:
                pruned[-1] = candidate
            continue
        pruned.append(candidate)
    return pruned


def _select_clips_globally_with_llm(
    client,
    model: str,
    *,
    candidates: List[Dict[str, Any]],
    duration_seconds: float,
    target_clip_count: int,
) -> Tuple[List[Dict[str, Any]], str]:
    if not client or not candidates:
        return [], ""

    payload_candidates: List[Dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, 1):
        payload_candidates.append(
            {
                "candidate_id": idx,
                "start": candidate.get("start"),
                "end": candidate.get("end"),
                "duration": round(float(candidate.get("end") or 0.0) - float(candidate.get("start") or 0.0), 2),
                "title": candidate.get("title") or "",
                "excerpt": candidate.get("excerpt") or "",
            }
        )

    system_prompt = (
        "Sen uzun bir Türkçe videodan seçilmiş klip adayları arasından en iyi Shorts planını kuran son seçici ajansın.\n"
        "Adaylar farklı pencerelerden geldiği için birbirini tekrar eden, aynı pasajı paylaşan veya sınırları eksik olan klipler olabilir.\n"
        "\n"
        "GÖREVİN:\n"
        "- En fazla target_clip_count kadar aday seç.\n"
        f"- {MIN_CLIP_SECONDS:.0f} saniyenin altındaki veya {MAX_CLIP_SECONDS:.0f} saniyenin üstündeki adayları seçme.\n"
        f"- İdeal klip süresi 40 ile 60 saniyedir; {MAX_CLIP_SECONDS:.0f} saniyeyi kapsamlı olduğu için ödüllendirme.\n"
        "- Yakın kopya veya büyük ölçüde çakışan adaylardan yalnızca BİRİNİ bırak.\n"
        "- Eğer iki aday aynı fikri taşıyorsa, daha tamamlanmış olanı ve cümle akışı daha güçlü olanı tercih et.\n"
        "- Video geneline yayılmış, birbirinden farklı ve en güçlü klipleri seç.\n"
        "- Sırf sayıyı doldurmak için zayıf aday seçme; daha az ama daha iyi seçim yap.\n"
        "\n"
        "SEÇİM KRİTERLERİ:\n"
        "- Açık fikir, güçlü tez, ikaz, duygu, vecize, hikaye veya çözüm taşıması\n"
        "- Tekrar etmemesi\n"
        "- Kendi içinde daha bütünlüklü olması\n"
        "- Aynı pasajın daha eksik/kesik versiyonunu elemesi\n"
        "\n"
        "ÇIKTI:\n"
        "Sadece geçerli JSON döndür.\n"
        "{\n"
        "  \"selected\": [\n"
        "    {\"candidate_id\": number, \"reason\": str},\n"
        "    ...\n"
        "  ]\n"
        "}\n"
        "reason kısa olsun; neden seçildiğini 1 cümlede belirt.\n"
    )
    payload = {
        "duration_seconds": duration_seconds,
        "target_clip_count": target_clip_count,
        "candidates": payload_candidates,
    }
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
        timeout=OPENAI_PLANNER_TIMEOUT_SECONDS,
    )
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
    except Exception:
        return [], raw
    selected = data.get("selected") or []
    chosen: List[Dict[str, Any]] = []
    seen_ids = set()
    for row in selected:
        try:
            candidate_id = int(row.get("candidate_id"))
        except Exception:
            continue
        if candidate_id in seen_ids or candidate_id < 1 or candidate_id > len(candidates):
            continue
        seen_ids.add(candidate_id)
        base = dict(candidates[candidate_id - 1])
        base["selector_reason"] = str(row.get("reason") or "").strip()
        chosen.append(base)
        if len(chosen) >= target_clip_count:
            break
    return chosen, raw


def propose_clips_with_agents(
    segments: List[Dict[str, Any]],
    transcript_text: str,
    duration_seconds: float,
    client,
    model: str,
    debug: bool = False,
    target_clip_count: int | None = None,
    plan_focus: str = "",
):
    """
    Returns (final_plan, debug_info)
    """
    debug_info: Dict[str, Any] = {
        "duration_seconds": duration_seconds,
        "windows": [],
        "window_raw_responses": [],
        "window_candidates": [],
        "deduped_candidates": [],
        "selector_input": {},
        "selector_raw_response": "",
        "final_plan": [],
        "target_clip_count": target_clip_count,
        "plan_focus": plan_focus,
        "normalized_segments_sample": [],
        "window_count": 0,
        "openai_call_count": 0,
        "produced_clip_count": 0,
        "kept_after_cap_count": 0,
        "clips_after_selector_count": 0,
    }

    if not client:
        return [], debug_info

    sentence_segments = merge_segments_into_sentences(segments)
    windows = build_windows(duration_seconds)
    debug_info["windows"] = windows
    debug_info["window_count"] = len(windows)

    all_candidates = []
    for idx, win in enumerate(windows, 1):
        win_segments = extract_segments_for_window(sentence_segments, win)
        normalized_segments = normalize_segments_for_agent(win_segments)
        debug_info["normalized_segments_sample"].append(normalized_segments[:3])
        clips, raw, llm_input = run_window_agent(
            client,
            model,
            win,
            normalized_segments,
            "",
            plan_focus=plan_focus,
        )
        agent1_clips = clips
        debug_info["openai_call_count"] += 1 + len(agent1_clips)
        debug_info["window_raw_responses"].append({"window": win, "raw": raw[:2000], "input": llm_input})
        accepted_clips: List[Dict[str, Any]] = []
        short_candidates: List[Dict[str, Any]] = []
        for clip in agent1_clips:
            dur = _clip_duration(clip)
            if dur >= MIN_CLIP_SECONDS:
                accepted_clips.append(clip)
            else:
                short_candidates.append(clip)

        fixed_clips: List[Dict[str, Any]] = []
        for candidate in short_candidates:
            fixed = _call_agent2_fix_clip(win, normalized_segments, candidate)
            debug_info["openai_call_count"] += 1
            if fixed is not None:
                fixed_clips.append(fixed)
                debug_info["openai_call_count"] += 1

        bounded_clips: List[Dict[str, Any]] = []
        for clip in accepted_clips + fixed_clips:
            bounded = _trim_candidate_to_duration_limit(clip, sentence_segments)
            if bounded is not None:
                bounded_clips.append(bounded)

        final_clips = bounded_clips
        debug_info["produced_clip_count"] += len(final_clips)
        final_clips_capped = final_clips[:2]
        debug_info["kept_after_cap_count"] += len(final_clips_capped)
        debug_info["window_candidates"].append(
            {
                "window": win,
                "seg_count": len(normalized_segments),
                "agent1_clips": agent1_clips,
                "final_clips": final_clips_capped,
                "final_clips_pre_cap": final_clips,
                "sample_text": " ".join((s.get("text") or "") for s in normalized_segments[:3])[:400],
                "llm_input": llm_input,
            }
        )
        all_candidates.extend(final_clips_capped)

    deduped = _dedupe_candidates_by_time(all_candidates)
    debug_info["deduped_candidates"] = deduped

    target_count = target_clip_count or _target_clip_count_for_duration(duration_seconds)
    selector_input = {
        "candidates": deduped,
        "duration_seconds": duration_seconds,
        "target_clip_count": target_count,
    }
    debug_info["selector_input"] = selector_input
    selected_candidates: List[Dict[str, Any]] = []
    selector_raw_response = ""
    if deduped:
        try:
            selected_candidates, selector_raw_response = _select_clips_globally_with_llm(
                client,
                model,
                candidates=deduped,
                duration_seconds=duration_seconds,
                target_clip_count=target_count,
            )
            debug_info["openai_call_count"] += 1
        except Exception:
            selected_candidates = []
            selector_raw_response = ""
    if not selected_candidates:
        selected_candidates = list(deduped[:target_count])

    aligned_candidates: List[Dict[str, Any]] = []
    for candidate in selected_candidates:
        snapped = _snap_clip_to_sentence_boundaries(candidate, sentence_segments)
        snapped = _trim_candidate_to_duration_limit(snapped, sentence_segments)
        if snapped is None:
            continue
        snapped["title"] = generate_clip_title(
            _clip_text_for_range(
                sentence_segments,
                snapped.get("start"),
                snapped.get("end"),
                excerpt=str(snapped.get("excerpt") or ""),
            ),
            language_hint="tr",
        )
        debug_info["openai_call_count"] += 1
        aligned_candidates.append(snapped)

    final_plan = _prune_overlapping_selected_clips(aligned_candidates)
    debug_info["clips_after_selector_count"] = len(final_plan)
    debug_info["selector_raw_response"] = selector_raw_response
    debug_info["final_plan"] = final_plan
    return final_plan, debug_info
