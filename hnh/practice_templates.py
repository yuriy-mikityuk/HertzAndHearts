"""Versioned practice-template JSON contract shared with recording applications."""
import copy
import json
from pathlib import Path

DEFAULT_TEMPLATES = [
    {"schema_version": 1, "id": "coherence", "revision": 1, "name": "Когерентность · 5 / 16 / 5",
     "before_minutes": 5, "practice_minutes": 16, "after_minutes": 5,
     "technique": "Когерентность на часах", "category": "breathing", "posture": "seated",
     "breathing": "other", "breathing_rate": None,
     "description": "Управляемое дыхание по программе часов. До и после — естественное дыхание.",
     "audio_mode": "voice", "automatic": True},
    {"schema_version": 1, "id": "breath-attention", "revision": 1, "name": "Внимание к дыханию · 5 / 15 / 5",
     "before_minutes": 5, "practice_minutes": 15, "after_minutes": 5,
     "technique": "Внимание к естественному дыханию", "category": "attention", "posture": "seated",
     "breathing": "natural", "breathing_rate": None,
     "description": "Наблюдение естественного дыхания без заданного темпа и задержек.",
     "audio_mode": "voice", "automatic": True},
]


def validate_template(item):
    if item.get("schema_version") != 1:
        raise ValueError("Неподдерживаемый формат шаблона")
    for key in ("name", "technique"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError("Заполните название шаблона и практики")
    for key in ("before_minutes", "practice_minutes", "after_minutes"):
        if type(item.get(key)) is not int or not 1 <= item[key] <= 120:
            raise ValueError("Длительность каждого этапа: целое число от 1 до 120 минут")
    if item.get("audio_mode") not in {"voice", "tone", "off"}:
        raise ValueError("Неизвестный режим звука")
    if item.get("category") not in {"breathing", "attention", "other"}:
        raise ValueError("Неизвестный тип практики")
    if item.get("posture") not in {"seated", "lying", "standing", "other"}:
        raise ValueError("Укажите позу")
    if item.get("breathing") not in {"natural", "paced", "other"}:
        raise ValueError("Укажите дыхание")
    if item["breathing"] == "paced":
        rate = item.get("breathing_rate")
        if not isinstance(rate, (float, int)) or isinstance(rate, bool) or not 1 <= rate <= 60:
            raise ValueError("Задайте частоту дыхания от 1 до 60 циклов в минуту")
    if not isinstance(item.get("automatic"), bool):
        raise ValueError("Некорректная настройка автоматических переходов")


def load_templates(store: Path):
    templates = {item["id"]: copy.deepcopy(item) for item in DEFAULT_TEMPLATES}
    errors = []
    for path in sorted((Path(store) / "practice-templates").glob("*.json")):
        try:
            item = json.loads(path.read_text())
            validate_template(item)
            if not isinstance(item.get("id"), str) or type(item.get("revision")) is not int or item["revision"] < 1:
                raise ValueError("Некорректный идентификатор или версия")
            if item["revision"] >= templates.get(item["id"], {}).get("revision", 0):
                templates[item["id"]] = item
        except (OSError, ValueError, TypeError, AttributeError) as error:
            errors.append(f"{path.name}: {error}")
    return sorted(templates.values(), key=lambda item: item["name"].casefold()), errors
