import json
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_RULES = {
    "keywords": [
        "[music]",
        "music",
        "muzik",
        "alkis",
        "applause",
        "silence",
        "sessizlik",
        "kahkaha",
        "laughter",
        "noise",
        "background noise",
        "sfx",
        "sound effect",
        "efekt",
    ],
    "non_speech_keywords": [],
    "regex_patterns": [
        "^\\s*\\[music\\]\\s*$",
        "^\\s*\\[applause\\]\\s*$",
        "^\\s*\\[laughter\\]\\s*$",
        "^\\s*\\[noise\\]\\s*$",
        "^\\s*\\[silence\\]\\s*$",
        "^\\s*\\[sfx\\]\\s*$",
        "^\\s*[\\u266a\\u266b]+\\s*$",
    ],
    "non_speech_regex_patterns": [],
    "min_chars": 3,
    "min_speech_chars": 3,
    "max_single_word_chars": 2,
    "max_non_speech_ratio": 0.4,
    "max_non_speech_ratio_inside_clip": 0.05,
    "qa_keywords": [
        "soru",
        "soran",
        "dinleyici",
        "kardesimiz",
    ],
    "conjunction_prefixes": [
        "ama",
        "fakat",
        "cunku",
        "ve",
        "binaenaleyh",
        "lakin",
        "ancak",
    ],
    "qa_max_seconds": 10.0,
    "padding_seconds": 10.0,
    "min_clip_seconds": 25.0,
    "max_clip_seconds": 60.0,
    "qa_max_question_seconds": 10.0,
    "target_clip_count_default": 10,
    "max_per_label": 3,
    "max_per_block": 2,
}


def _rules_path() -> Path:
    return Path(__file__).resolve().parent.parent / "rules" / "non_speech_rules.json"


def load_non_speech_rules() -> Dict[str, Any]:
    path = _rules_path()
    if not path.exists():
        return dict(DEFAULT_RULES)
    try:
        data = json.loads(path.read_text())
    except Exception:
        return dict(DEFAULT_RULES)
    rules = dict(DEFAULT_RULES)
    rules.update({k: v for k, v in data.items() if v is not None})
    if not rules.get("non_speech_keywords"):
        rules["non_speech_keywords"] = list(rules.get("keywords") or [])
    if not rules.get("non_speech_regex_patterns"):
        rules["non_speech_regex_patterns"] = list(rules.get("regex_patterns") or [])
    return rules


def save_non_speech_rules(rules: Dict[str, Any]) -> None:
    path = _rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(DEFAULT_RULES)
    payload.update(rules or {})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def add_non_speech_keyword(keyword: str) -> Dict[str, Any]:
    keyword = (keyword or "").strip()
    if not keyword:
        return load_non_speech_rules()
    rules = load_non_speech_rules()
    keywords: List[str] = list(rules.get("keywords") or [])
    if keyword not in keywords:
        keywords.append(keyword)
    rules["keywords"] = keywords
    save_non_speech_rules(rules)
    return rules
