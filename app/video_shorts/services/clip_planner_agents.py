import json
import math
from typing import List, Dict, Any, Tuple, Optional

from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.clip_plan_focus_prompts import (
    get_agent_focus_block,
    get_focus_categories_agent_block,
    get_focus_categories_selector_block,
    normalize_planning_language,
)
from app.video_shorts.services.clip_title import generate_clip_title

OPENAI_PLANNER_TIMEOUT_SECONDS = 45.0
MIN_CLIP_SECONDS = 25.0
MAX_CLIP_SECONDS = 60.0

_FIX_CLIP_SYSTEM_PROMPTS = {
    "tr": (
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
        "   - Süre 60 saniyeyi KESİNLİKLE geçmemelidir.\n"
        "   - İdeal aralık 35 ile 55 saniye arasıdır.\n"
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
    ),
    "en": (
        "You are a clip fixer agent.\n"
        "You receive three inputs:\n"
        "- window: {\"start\": float, \"end\": float}\n"
        "- segments: a time-ordered list of sentence blocks inside this window. Each segment:\n"
        "    {\"start\": float, \"end\": float, \"duration\": float, \"text\": str}\n"
        "- base_clip: the clip candidate proposed by Agent 1. Shape:\n"
        "    {\"title\": str, \"start\": float, \"end\": float, \"excerpt\": str}\n"
        "\n"
        "Video language is English.\n"
        "Your job is to preserve the same idea core as base_clip and, when needed, expand it backward or forward\n"
        "so the clip becomes at least 25 seconds long.\n"
        "\n"
        "HARD RULES:\n"
        "1) The clip must always stay inside window.start and window.end.\n"
        "   - new_start >= window.start\n"
        "   - new_end   <= window.end\n"
        "\n"
        "2) The base clip must remain inside the new range.\n"
        "   - new_start <= base_clip.start\n"
        "   - new_end   >= base_clip.end\n"
        "\n"
        "3) Start and end may only sit on segment boundaries.\n"
        "   - new_start must exactly match one segment.start\n"
        "   - new_end   must exactly match one segment.end\n"
        "   - All segments between them belong to the clip, and the clip must contain AT LEAST 2 segments.\n"
        "\n"
        "4) Duration rule:\n"
        "   - Duration = new_end - new_start\n"
        "   - Duration must NEVER be below 25 seconds.\n"
        "   - Duration must NEVER exceed 60 seconds.\n"
        "   - Ideal range is 35 to 55 seconds.\n"
        "\n"
        "5) Expansion logic:\n"
        "   - First find the segment span already covered by base_clip.\n"
        "   - If that span is already >= 25 seconds, keep it and just produce a clean excerpt.\n"
        "   - If duration < 25, first add segments BEFORE the base clip one by one.\n"
        "     If that is still not enough, add segments AFTER the base clip.\n"
        "   - While expanding, preserve topic and idea continuity. Do not jump to a different subject.\n"
        "\n"
        "6) If you cannot reach 25 seconds inside the same idea and within window bounds,\n"
        "   produce NO clip.\n"
        "\n"
        "7) Return only ONE clip. This call exists only to fix one base_clip.\n"
        "\n"
        "OUTPUT FORMAT RULE:\n"
        "Return valid JSON only. No explanations, no commentary.\n"
        "{\n"
        "  \"clips\": [\n"
        "    {\"start\": float, \"end\": float, \"excerpt\": str}\n"
        "  ]\n"
        "}\n"
        "\n"
        "start, end: timestamps that obey all rules above.\n"
        "excerpt: a strong quote of 1 to 3 sentences taken from the chosen segments.\n"
        "If no valid expansion exists, return:\n"
        "{ \"clips\": [] }\n"
    ),
}

_WINDOW_AGENT_SYSTEM_PROMPTS = {
    "tr": (
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
        "{focus_block}"
        "\n"
        "EN ÖNEMLİ KURALLAR:\n"
        "1) Klipler TEK CÜMLE olmayacak, küçük bir sohbet/paragraf bloğu olacak.\n"
        "   Her klip birden fazla segment içermeli (genelde 2 ile 6 segment arası).\n"
        "2) Kesin kural: Bir klipte (end - start) 25 saniyeden KESİNLİKLE az olmayacak.\n"
        "   - Eğer güçlü bir cümle kısa sürüyorsa, aynı window içindeki ÖNCEKİ ve SONRAKİ segmentleri de ekleyerek\n"
        "     süreyi en az 25 saniyeye çıkaracaksın.\n"
        "   - 25 saniyenin altında kalan klip üretmeyeceksin.\n"
        "   - Bir klip 60 saniyeyi KESİNLİKLE geçmeyecek. 60 saniyenin üstüne çıkan klip üretmeyeceksin.\n"
        "\n"
        "Süre kuralları:\n"
        "- İdeal klip süresi 35 ile 55 saniye arasındadır.\n"
        "- Süreyi hesaplarken seçtiğin segmentleri kullan:\n"
        "  * Klipte dahil ettiğin ilk segmentin start değeri klibin start değeri olsun.\n"
        "  * Klipte dahil ettiğin son segmentin end değeri klibin end değeri olsun.\n"
        "  * Klip süresi = end - start.\n"
        "- Sert üst sınır 60 saniyedir. 60 saniyeyi aşan klip geçersizdir.\n"
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
        "- Eğer (e - s) > 60 ise bu klibi KULLANMA.\n"
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
    ),
    "en": (
        "You are a clip planning agent. Video language is English. From the given sentence segments, "
        "you propose meaningful mini-conversation clips for YouTube Shorts.\n"
        "\n"
        "Input structure:\n"
        "- 'segments' is a time-ordered list of sentence blocks. Each item has: start, end, duration (seconds), text.\n"
        "- 'window.start' and 'window.end' mark the core time range for this agent. "
        "Every clip you choose must stay inside that range.\n"
        "\n"
        "Density types you are looking for:\n"
        "- tez: core claims, framing lines, and sentences carrying the main idea\n"
        "- ikaz: sharp warnings, danger signals, and high-stakes caution\n"
        "- vecize: short, quotable insights or compact takeaways\n"
        "- hikaye: story beats, anecdotes, scenes, and narrated events\n"
        "- cozum: answers to 'what should we do?', practical direction, and next steps\n"
        "- duygu: explicit feeling, grief, awe, anger, love, or longing\n"
        "{focus_block}"
        "\n"
        "MOST IMPORTANT RULES:\n"
        "1) Clips must NOT be a single sentence. Each one should feel like a small conversation or paragraph block.\n"
        "   Every clip should contain multiple segments, usually between 2 and 6.\n"
        "2) Hard rule: a clip's duration (end - start) must NEVER be below 25 seconds.\n"
        "   - If a strong sentence is too short on its own, extend it by adding nearby PREVIOUS and NEXT segments inside the window.\n"
        "   - Do not produce any clip below 25 seconds.\n"
        "   - A clip must NEVER exceed 60 seconds. Do not produce anything above 60 seconds.\n"
        "\n"
        "Duration rules:\n"
        "- Ideal clip length is between 35 and 55 seconds.\n"
        "- Compute duration from the segments you include:\n"
        "  * The start of the first included segment becomes the clip start.\n"
        "  * The end of the last included segment becomes the clip end.\n"
        "  * Clip duration = end - start.\n"
        "- The hard upper limit is 60 seconds. Anything above 60 seconds is invalid.\n"
        "  The clip should feel natural, but it must never cross that limit.\n"
        "\n"
        "Clip construction steps:\n"
        "1) Find a strong core sentence inside the window that carries tez, ikaz, vecize, hikaye, cozum, or duygu.\n"
        "2) Build context around that core:\n"
        "   - Add earlier segments that set up the idea.\n"
        "   - Add later segments that complete the meaning.\n"
        "3) Use that expansion to reach at least 25 seconds:\n"
        "   - Combine 3-4 segments when needed, and keep extending until it becomes a natural mini-conversation.\n"
        "4) The clip must not start mid-sentence. Its opening should be the start of a natural sentence.\n"
        "   If the first segment begins with a connector like 'and', 'but', or 'so', include the segment before it.\n"
        "   The clip must not end mid-sentence. If the final segment ends with a comma, unfinished thought, or open connector,\n"
        "   add ONLY the next sentence that completes that thought; do not keep appending until a new topic starts.\n"
        "5) Do not multiply clips that repeat the same message or use nearly the same core.\n"
        "   Merge similar density into one stronger clip.\n"
        "6) You may produce at most 2 clips in this window.\n"
        "   Prefer fewer, deeper clips over many short ones.\n"
        "\n"
        "CHECK STEP (DURATION):\n"
        "Before returning a clip, check this mentally:\n"
        "- The first included segment starts at s\n"
        "- The last included segment ends at e\n"
        "- If (e - s) < 25, DO NOT USE that clip. Instead:\n"
        "  * First try adding neighboring segments until duration goes above 25 seconds.\n"
        "  * If it still stays under 25, cancel that clip entirely.\n"
        "- If (e - s) > 60, DO NOT USE that clip.\n"
        "\n"
        "CHECK STEP (OVERLAP):\n"
        "- Do not create two different clips that use nearly the same segments inside the same ~30 second area.\n"
        "- If two candidate clips share most of the same segments or the same main idea,\n"
        "  keep only the strongest and fullest one, and cancel the other completely.\n"
        "\n"
        "Output format:\n"
        "Return valid JSON only. Use this shape:\n"
        "{\n"
        '  \"clips\": [\n'
        '    {\"start\": float, \"end\": float, \"excerpt\": str},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "\n"
        "excerpt: a sharp 1-3 sentence quote from inside the clip. "
        "It should stand alone and let the viewer feel what they are about to watch.\n"
    ),
}

_SELECTOR_SYSTEM_PROMPTS = {
    "tr": (
        "Sen uzun bir Türkçe videodan seçilmiş klip adayları arasından en iyi Shorts planını kuran son seçici ajansın.\n"
        "Adaylar farklı pencerelerden geldiği için birbirini tekrar eden, aynı pasajı paylaşan veya sınırları eksik olan klipler olabilir.\n"
        "\n"
        "GÖREVİN:\n"
        "- En fazla target_clip_count kadar aday seç.\n"
        f"- {MIN_CLIP_SECONDS:.0f} saniyenin altındaki veya {MAX_CLIP_SECONDS:.0f} saniyenin üstündeki adayları seçme.\n"
        f"- İdeal klip süresi 35 ile 55 saniyedir; {MAX_CLIP_SECONDS:.0f} saniyeyi geçen hiçbir adayı ödüllendirme.\n"
        "- Yakın kopya veya büyük ölçüde çakışan adaylardan yalnızca BİRİNİ bırak.\n"
        "- Eğer iki aday aynı fikri taşıyorsa, daha tamamlanmış olanı ve cümle akışı daha güçlü olanı tercih et.\n"
        "- Video geneline yayılmış, birbirinden farklı ve en güçlü klipleri seç.\n"
        "- Sırf sayıyı doldurmak için zayıf aday seçme; daha az ama daha iyi seçim yap.\n"
        "\n"
        "{focus_block}"
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
    ),
    "en": (
        "You are the final selector agent building the best Shorts plan from clip candidates extracted out of a longer English video.\n"
        "Because candidates come from different windows, some may repeat each other, share the same passage, or have weaker boundaries.\n"
        "\n"
        "YOUR JOB:\n"
        "- Select at most target_clip_count candidates.\n"
        f"- Do not select anything shorter than {MIN_CLIP_SECONDS:.0f} seconds or longer than {MAX_CLIP_SECONDS:.0f} seconds.\n"
        f"- Ideal clip length is 35 to 55 seconds; do not reward any candidate above {MAX_CLIP_SECONDS:.0f} seconds.\n"
        "- If two candidates are near-duplicates or heavily overlap, keep only ONE.\n"
        "- If two candidates carry the same idea, prefer the more complete one with stronger sentence flow.\n"
        "- Pick the strongest clips spread across the full video rather than clustering on one passage.\n"
        "- Do not fill the quota with weak candidates; fewer but better is the right tradeoff.\n"
        "\n"
        "{focus_block}"
        "SELECTION CRITERIA:\n"
        "- Carries a clear idea, strong thesis, warning, emotion, maxim, story, or solution\n"
        "- Does not repeat what another selected clip already covers\n"
        "- Feels more internally complete\n"
        "- Beats weaker or more truncated versions of the same passage\n"
        "\n"
        "OUTPUT:\n"
        "Return valid JSON only.\n"
        "{\n"
        "  \"selected\": [\n"
        "    {\"candidate_id\": number, \"reason\": str},\n"
        "    ...\n"
        "  ]\n"
        "}\n"
        "Keep reason short; explain selection in one sentence.\n"
    ),
}


def _build_fix_clip_system_prompt(language: str) -> str:
    return _FIX_CLIP_SYSTEM_PROMPTS[normalize_planning_language(language)]


def _build_window_agent_system_prompt(language: str, focus_block: str) -> str:
    prompt = _WINDOW_AGENT_SYSTEM_PROMPTS[normalize_planning_language(language)]
    return prompt.replace("{focus_block}", focus_block)


def _build_selector_system_prompt(language: str, focus_block: str) -> str:
    prompt = _SELECTOR_SYSTEM_PROMPTS[normalize_planning_language(language)]
    return prompt.replace("{focus_block}", focus_block)


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


def _ends_with_sentence_terminal(text: str) -> bool:
    stripped = (text or "").strip().rstrip('"\')]}»”’ ')
    return bool(stripped) and stripped.endswith((".", "!", "?", "…"))


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


def _call_agent2_fix_clip(
    window: Dict[str, Any],
    segments: List[Dict[str, Any]],
    candidate_clip: Dict[str, Any],
    *,
    language: str = "tr",
) -> Optional[Dict[str, Any]]:
    if not _openai_client:
        return None
    
    resolved_language = normalize_planning_language(language)
    system_prompt = _build_fix_clip_system_prompt(resolved_language)

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
            language_hint=resolved_language,
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
    focus_categories: Optional[List[str]] = None,
    language: str = "tr",
) -> Tuple[List[Dict[str, Any]], str, str]:
    resolved_language = normalize_planning_language(language)
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
    focus_block = get_agent_focus_block(plan_focus) + get_focus_categories_agent_block(
        focus_categories,
        resolved_language,
    )
    system_prompt = _build_window_agent_system_prompt(resolved_language, focus_block)
 

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
            language_hint=resolved_language,
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

    end_text_complete = _ends_with_sentence_terminal(str(end_seg.get("text") or ""))
    if duration <= max_seconds and end_text_complete:
        return trimmed

    if end_text_complete:
        for candidate_end_idx in range(end_idx, start_idx - 1, -1):
            if not _ends_with_sentence_terminal(str(sentence_segments[candidate_end_idx].get("text") or "")):
                continue
            start = float(sentence_segments[start_idx].get("start") or raw_start)
            end = float(sentence_segments[candidate_end_idx].get("end") or raw_end)
            candidate_duration = end - start
            if candidate_duration < min_seconds:
                break
            if candidate_duration <= max_seconds:
                return dict(trimmed, start=round(start, 2), end=round(end, 2))

        for candidate_end_idx in range(end_idx, start_idx - 1, -1):
            if not _ends_with_sentence_terminal(str(sentence_segments[candidate_end_idx].get("text") or "")):
                continue
            end = float(sentence_segments[candidate_end_idx].get("end") or raw_end)
            for candidate_start_idx in range(start_idx + 1, candidate_end_idx + 1):
                start = float(sentence_segments[candidate_start_idx].get("start") or raw_start)
                candidate_duration = end - start
                if candidate_duration > max_seconds:
                    continue
                if candidate_duration < min_seconds:
                    break
                return dict(trimmed, start=round(start, 2), end=round(end, 2))
        return None

    completion_end_idx = None
    for idx in range(end_idx, len(sentence_segments)):
        if _ends_with_sentence_terminal(str(sentence_segments[idx].get("text") or "")):
            completion_end_idx = idx
            break
    if completion_end_idx is None:
        return None

    completed_end = float(sentence_segments[completion_end_idx].get("end") or raw_end)
    for candidate_start_idx in range(start_idx, completion_end_idx + 1):
        start = float(sentence_segments[candidate_start_idx].get("start") or raw_start)
        candidate_duration = completed_end - start
        if candidate_duration > max_seconds:
            continue
        if candidate_duration < min_seconds:
            break
        return dict(trimmed, start=round(start, 2), end=round(completed_end, 2))
    return None


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
    focus_categories: Optional[List[str]] = None,
    language: str = "tr",
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

    resolved_language = normalize_planning_language(language)
    system_prompt = _build_selector_system_prompt(
        resolved_language,
        get_focus_categories_selector_block(focus_categories, resolved_language),
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
    focus_categories: Optional[List[str]] = None,
    language: str = "tr",
):
    """
    Returns (final_plan, debug_info)
    """
    resolved_language = normalize_planning_language(language)
    debug_info: Dict[str, Any] = {
        "duration_seconds": duration_seconds,
        "language": resolved_language,
        "windows": [],
        "window_raw_responses": [],
        "window_candidates": [],
        "deduped_candidates": [],
        "selector_input": {},
        "selector_raw_response": "",
        "final_plan": [],
        "target_clip_count": target_clip_count,
        "plan_focus": plan_focus,
        "focus_categories": focus_categories or [],
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
            focus_categories=focus_categories,
            language=resolved_language,
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
            fixed = _call_agent2_fix_clip(win, normalized_segments, candidate, language=resolved_language)
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
                focus_categories=focus_categories,
                language=resolved_language,
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
            language_hint=resolved_language,
        )
        debug_info["openai_call_count"] += 1
        aligned_candidates.append(snapped)

    final_plan = _prune_overlapping_selected_clips(aligned_candidates)
    debug_info["clips_after_selector_count"] = len(final_plan)
    debug_info["selector_raw_response"] = selector_raw_response
    debug_info["final_plan"] = final_plan
    return final_plan, debug_info
