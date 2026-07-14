import json
import re
import unicodedata
from typing import List, Dict, Any, Tuple, Optional

from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.clip_plan_focus_prompts import get_agent_focus_block

OPENAI_PLANNER_TIMEOUT_SECONDS = 45.0
_TITLE_TOKEN_RE = re.compile(r"[A-Za-zÇĞİIÖŞÜçğıiöşü]+(?:['’][A-Za-zÇĞİIÖŞÜçğıiöşü]+)?")


def _normalize_for_match(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.casefold()


def _is_entity_like_token(token: str) -> bool:
    cleaned = re.sub(r"['’].*$", "", (token or "").strip())
    if len(cleaned) < 3:
        return False
    if cleaned.isupper():
        return True
    first = cleaned[:1]
    rest = cleaned[1:]
    return first.isupper() and any(ch.islower() for ch in rest)


def _extract_entity_like_title_tokens(title: str) -> List[str]:
    return [
        token
        for token in _TITLE_TOKEN_RE.findall(title or "")
        if _is_entity_like_token(token)
    ]


def _build_grounded_title_fallback(excerpt: str, default: str = "Bu Klipte Ne Anlatılıyor?") -> str:
    text = " ".join((excerpt or "").strip().split())
    if not text:
        return default
    first_sentence = re.split(r"[.!?…]+", text, maxsplit=1)[0].strip(" \"'“”‘’")
    candidate = first_sentence or text
    if len(candidate) > 80:
        truncated = candidate[:80].rsplit(" ", 1)[0].strip()
        candidate = truncated or candidate[:80].strip()
    return candidate or default


def _ground_title_to_transcript(title: str, transcript_text: str, fallback_excerpt: str) -> str:
    candidate = " ".join((title or "").strip().split())
    if not candidate:
        return _build_grounded_title_fallback(fallback_excerpt)
    transcript_norm = _normalize_for_match(transcript_text)
    if not transcript_norm:
        return candidate
    missing_tokens = [
        token
        for token in _extract_entity_like_title_tokens(candidate)
        if _normalize_for_match(re.sub(r"['’].*$", "", token)) not in transcript_norm
    ]
    if not missing_tokens:
        return candidate
    return _build_grounded_title_fallback(fallback_excerpt)


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
        "   - İdeal aralık 25 ile 60 saniye arasıdır. 60 a biraz geçmesi kabul edilebilir ama gerekmedikçe uzatma.\n"
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
        "    {\"title\": str, \"start\": float, \"end\": float, \"excerpt\": str}\n"
        "  ]\n"
        "}\n"
        "\n"
        "title: Base klibin ana fikrini yansıtan, kısa ve vurucu bir Türkçe başlık.\n"
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
    if duration < 25:
        return None
    return {
        "title": clip_data.get("title") or candidate_clip.get("title") or "",
        "start": round(start, 2),
        "end": round(end, 2),
        "excerpt": clip_data.get("excerpt") or candidate_clip.get("excerpt") or "",
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
        "\n"
        "Süre kuralları:\n"
        "- İdeal klip süresi 40 ile 60 saniye arasındadır.\n"
        "- Süreyi hesaplarken seçtiğin segmentleri kullan:\n"
        "  * Klipte dahil ettiğin ilk segmentin start değeri klibin start değeri olsun.\n"
        "  * Klipte dahil ettiğin son segmentin end değeri klibin end değeri olsun.\n"
        "  * Klip süresi = end - start.\n"
        "- Süre 60 saniyenin biraz üstüne çıkarsa bu kabul edilebilir, ama keyfi sabit aralıklar kullanma.\n"
        "  Her klibin süresi doğal akıştan gelsin.\n"
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
        "5) Aynı mesajı tekrar eden, neredeyse aynı çekirdeği taşıyan klipleri çoğaltma.\n"
        "   Benzer yoğunlukları tek klipte topla.\n"
        "6) Bu window içinde anlamlı bulduğun kadar klip üretebilirsin.\n"
        "   Çok sayıda kısa klip yerine az sayıda dolu ve derin klip tercih et.\n"
        "\n"
        "KONTROL ADIMI (SÜRE):\n"
        "Her klibi döndürmeden önce şu kontrolü zihninde yap:\n"
        "- Klipte kullandığın ilk segmentin start değeri = s\n"
        "- Klipte kullandığın son segmentin end değeri = e\n"
        "- Eğer (e - s) < 25 ise bu klibi KULLANMA. Bu durumda:\n"
        "  * Önce komşu segmentler eklenerek süreyi 25 saniyenin üstüne çıkarmaya çalış.\n"
        "  * Hala 25 saniyenin altında kalıyorsa, o klibi tamamen iptal et.\n"
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
        '    {\"title\": str, \"start\": float, \"end\": float, \"excerpt\": str},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "\n"
        "title: Kısa, Türkçe ve klibin ana mesajını taşıyan bir başlık olsun.\n"
        "SADECE klibin kendi segmentlerinde AÇIKÇA geçen bilgiyi kullan. "
        "Metinde geçmeyen hiçbir kişi, siyasi parti, kurum, kuruluş, yer, tarih "
        "veya olay adını başlığa EKLEME. Metinde bir isim yoksa başlıkta da olmasın. "
        "Başlık merak uyandırsın ve izleyiciyi durduracak güçte olsun, ama bu asla "
        "doğruluktan taviz vermek pahasına olmayacak.\n"
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
    transcript_scope_text = " ".join(
        (s.get("text") or "").strip() for s in (segments or []) if (s.get("text") or "").strip()
    )
    for clip in clips:
        fallback_excerpt = str(clip.get("excerpt") or excerpt_text or transcript_scope_text or "").strip()
        clip["title"] = _ground_title_to_transcript(
            str(clip.get("title") or ""),
            transcript_scope_text or excerpt_text,
            fallback_excerpt,
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
    }

    if not client:
        return [], debug_info

    sentence_segments = merge_segments_into_sentences(segments)
    windows = build_windows(duration_seconds)
    debug_info["windows"] = windows

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
        debug_info["window_raw_responses"].append({"window": win, "raw": raw[:2000], "input": llm_input})
        agent1_clips = clips
        accepted_clips: List[Dict[str, Any]] = []
        short_candidates: List[Dict[str, Any]] = []
        for clip in agent1_clips:
            dur = None
            try:
                dur = float(clip.get("end", 0) or 0) - float(clip.get("start", 0) or 0)
            except Exception:
                dur = None
            if dur is not None and dur >= 25:
                accepted_clips.append(clip)
            else:
                short_candidates.append(clip)

        fixed_clips: List[Dict[str, Any]] = []
        for candidate in short_candidates:
            fixed = _call_agent2_fix_clip(win, normalized_segments, candidate)
            if fixed is not None:
                fixed_clips.append(fixed)

        final_clips = accepted_clips + fixed_clips
        debug_info["window_candidates"].append(
            {
                "window": win,
                "seg_count": len(normalized_segments),
                "agent1_clips": agent1_clips,
                "final_clips": final_clips,
                "sample_text": " ".join((s.get("text") or "") for s in normalized_segments[:3])[:400],
                "llm_input": llm_input,
            }
        )
        all_candidates.extend(final_clips)

    deduped = _dedupe_candidates_by_time(all_candidates)
    debug_info["deduped_candidates"] = deduped

    selector_input = {
        "candidates": deduped,
        "duration_seconds": duration_seconds,
    }
    debug_info["selector_input"] = selector_input
    final_plan = sorted(deduped, key=lambda c: c.get("start", 0) or 0)
    debug_info["selector_raw_response"] = ""
    debug_info["final_plan"] = final_plan
    return final_plan, debug_info
