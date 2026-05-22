import json
from pathlib import Path
from typing import Any, Dict


def _rules_path() -> Path:
    return Path(__file__).resolve().parent.parent / "rules" / "planner_rules_v4.json"


def load_planner_rules_v4() -> Dict[str, Any]:
    path = _rules_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}
