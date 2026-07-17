import logging
import json

from typing import Dict, List


logger = logging.getLogger(__name__)


_FOCUS_CONFIGS: Dict[str, Dict[str, str]] = {
    "anecdote_memory_scene": {
        "label": "Kıssa / hatıra / sahne",
        "agent_block_tr": (
            "\n"
            "ODAK MODU: kissa_hatira_sahne\n"
            "- Önce transcript içindeki en güçlü kıssa, hatıra ve sahne adaylarını belirle.\n"
            "- Final seçimde sadece en anlatı değeri yüksek ve tek başına anlaşılabilen parçaları al.\n"
            "- Bu çağrıda yalnızca kıssa, hatıra, sahne anlatımı veya sakin olay akışı taşıyan bölümleri seç.\n"
            "- Özellikle geçmişte yaşanmış bir olay, bir kişi etrafında gelişen anlatım, küçük bir sahne hissi veya yumuşak bir ders barındıran parçaları tercih et.\n"
            "- Doğrudan tez anlatan, sert ikaz içeren, polemiğe giren, teknik açıklama yapan veya yüksek gerilimli bölümleri alma.\n"
            "- Seçilen klip, tek başına dinlenince de anlaşılmalı ve kendi içinde küçük ama tamamlanmış bir anlatı hissi vermeli.\n"
            "- Birbirine çok benzeyen, aynı fikri tekrar eden veya benzer cümle yapısıyla gelen klipleri birlikte seçme.\n"
            "- Her seçilen klip farklı bir sahne, hatıra veya anlatı değeri taşısın.\n"
            "- Başlıklar sakin, yumuşak ve merak uyandıran tonda olsun; sloganvari veya sert başlık yazma.\n"
        ),
        "llm_block_tr": (
            "ODAK MODU:\n"
            "- Önce transcript içindeki en güçlü kıssa, hatıra ve sahne adaylarını belirle.\n"
            "- Final seçimde sadece en anlatı değeri yüksek ve tek başına anlaşılabilen parçaları al.\n"
            "- Yalnızca kıssa, hatıra, sahne anlatımı veya sakin olay akışı taşıyan bölümleri seç.\n"
            "- Özellikle geçmişte yaşanmış bir olay, bir kişi etrafında gelişen anlatım, küçük bir sahne hissi veya yumuşak bir ders barındıran parçaları tercih et.\n"
            "- Doğrudan tez anlatan, sert ikaz içeren, polemiğe giren, teknik açıklama yapan veya yüksek gerilimli bölümleri alma.\n"
            "- Her seçilen klip, tek başına dinlenince de anlaşılmalı ve kendi içinde küçük ama tamamlanmış bir anlatı hissi vermeli.\n"
            "- Birbirine çok benzeyen, aynı fikri tekrar eden veya benzer cümle yapısıyla gelen klipleri birlikte seçme.\n"
            "- Her seçilen klip farklı bir sahne, hatıra veya anlatı değeri taşısın.\n"
            "- Başlıklar sakin, yumuşak ve merak uyandıran tonda olsun; sloganvari veya sert başlık yazma.\n\n"
        ),
    }
}

FOCUS_CATEGORY_OPTIONS: List[Dict[str, str]] = [
    {"key": "tez", "label": "Tez", "description": "ana fikri taşıyan, hüküm ve çerçeve cümleleri"},
    {"key": "ikaz", "label": "İkaz", "description": "sarsıcı uyarılar, tehlike vurguları, güçlü ikazlar"},
    {"key": "vecize", "label": "Vecize", "description": "kısa, duvara asılacak özlü sözler ve yoğun çıkarımlar"},
    {"key": "hikaye", "label": "Hikaye", "description": "kişi, yer, olay veya hatıra anlatan bölümler"},
    {"key": "cozum", "label": "Çözüm", "description": "\"Ne yapmalı?\" sorusuna cevap veren, yol haritası çizen bölümler"},
    {"key": "duygu", "label": "Duygu", "description": "açık duygu içeren, sitem, acı, hayret, öfke veya sevgi taşıyan ifadeler"},
]
ALL_FOCUS_CATEGORIES: List[str] = [item["key"] for item in FOCUS_CATEGORY_OPTIONS]
_FOCUS_CATEGORY_KEYS = set(ALL_FOCUS_CATEGORIES)


def normalize_plan_focus(plan_focus: str) -> str:
    normalized = (plan_focus or "").strip().lower()
    if not normalized:
        return ""
    if normalized in _FOCUS_CONFIGS:
        return normalized
    logger.warning("Unknown clip plan focus received: %s", normalized)
    return ""


def normalize_focus_categories(raw_categories, *, default_to_all: bool = True) -> List[str]:
    parsed: List[str] = []
    if raw_categories is None:
        parsed = []
    elif isinstance(raw_categories, (list, tuple, set)):
        parsed = [str(item or "").strip().lower() for item in raw_categories]
    else:
        raw_text = str(raw_categories or "").strip()
        if raw_text.startswith("[") and raw_text.endswith("]"):
            try:
                loaded = json.loads(raw_text)
            except Exception:
                loaded = None
            if isinstance(loaded, list):
                parsed = [str(item or "").strip().lower() for item in loaded]
            else:
                parsed = [part.strip().lower() for part in raw_text.split(",")]
        else:
            parsed = [part.strip().lower() for part in raw_text.split(",")]

    ordered: List[str] = []
    seen = set()
    for key in ALL_FOCUS_CATEGORIES:
        if key in parsed and key not in seen:
            ordered.append(key)
            seen.add(key)
    if ordered:
        return ordered
    return list(ALL_FOCUS_CATEGORIES) if default_to_all else []


def get_plan_focus_config(plan_focus: str) -> Dict[str, str]:
    normalized = normalize_plan_focus(plan_focus)
    if not normalized:
        return {}
    return _FOCUS_CONFIGS[normalized]


def get_plan_focus_label(plan_focus: str) -> str:
    return get_plan_focus_config(plan_focus).get("label") or ""


def get_agent_focus_block(plan_focus: str) -> str:
    return get_plan_focus_config(plan_focus).get("agent_block_tr") or ""


def get_llm_focus_block(plan_focus: str) -> str:
    return get_plan_focus_config(plan_focus).get("llm_block_tr") or ""


def get_focus_categories_agent_block(raw_categories) -> str:
    categories = normalize_focus_categories(raw_categories)
    if categories == ALL_FOCUS_CATEGORIES:
        return ""
    selected = [
        f"{item['key']}: {item['description']}"
        for item in FOCUS_CATEGORY_OPTIONS
        if item["key"] in categories
    ]
    ignored = [
        item["key"]
        for item in FOCUS_CATEGORY_OPTIONS
        if item["key"] not in categories
    ]
    return (
        "\n"
        "ODAK KATEGORİLERİ:\n"
        f"- Bu çağrıda SADECE şu yoğunluk türlerini ara: {', '.join(categories)}.\n"
        f"- Seçili kategorilerin anlamı: {'; '.join(selected)}.\n"
        f"- Şu kategorileri tamamen yok say: {', '.join(ignored)}.\n"
        "- Eğer güçlü bir pasaj seçili olmayan kategoriye giriyorsa, onu klip adayı yapma.\n"
    )


def get_focus_categories_selector_block(raw_categories) -> str:
    categories = normalize_focus_categories(raw_categories)
    if categories == ALL_FOCUS_CATEGORIES:
        return ""
    ignored = [
        item["key"]
        for item in FOCUS_CATEGORY_OPTIONS
        if item["key"] not in categories
    ]
    return (
        "ODAK KATEGORİLERİ:\n"
        f"- Bu seçim turunda yalnızca şu yoğunluk türlerini ödüllendir: {', '.join(categories)}.\n"
        f"- Şu türleri taşıyan adayları geriye it veya ele: {', '.join(ignored)}.\n"
        "- Bir aday teknik olarak güçlü olsa bile seçili kategoriye uymuyorsa üst sıralara alma.\n\n"
    )
