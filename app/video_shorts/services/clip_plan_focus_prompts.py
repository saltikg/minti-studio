import logging

from typing import Dict


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


def normalize_plan_focus(plan_focus: str) -> str:
    normalized = (plan_focus or "").strip().lower()
    if not normalized:
        return ""
    if normalized in _FOCUS_CONFIGS:
        return normalized
    logger.warning("Unknown clip plan focus received: %s", normalized)
    return ""


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
