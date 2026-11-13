import json
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


def load_json(path: str) -> Any:
    file_path = Path(path)
    try:
        raw_json = file_path.read_text("utf-8")
    except (FileNotFoundError, UnicodeDecodeError, OSError, PermissionError) as e:
        raise ConfigError(f"load_json: can not read {path}, {e}")
    json_obj = json.loads(raw_json)
    return json_obj
