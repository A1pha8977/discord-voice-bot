import os
from pathlib import Path

from dotenv import load_dotenv

_dotenv_loaded = False


def load_env(key: str) -> str | None:
    if not key.strip():
        raise ValueError("key must be a nonblank string")

    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        _dotenv_loaded = True
    return os.getenv(key)
