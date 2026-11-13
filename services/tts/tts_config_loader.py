"""Build voice configurations from the project JSON schema."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from services.tts.deepseek_tts_client import DeepseekTTSConfig
from services.tts.fish_audio_tts_client import FishAudioTTSConfig
from services.tts.tts_config import TTSConfig
from utils.config.json_loader import ConfigError, load_json

_CONFIG_BUILDERS: Mapping[str, Callable[..., TTSConfig]] = {
    "DEEPSEEK": DeepseekTTSConfig,
    "FISH_AUDIO": FishAudioTTSConfig,
}


def load_tts_configs(path: Path) -> list[TTSConfig]:
    """Build voice configurations from a JSON file.

    The file holds a list of objects with ``provider_id``, ``voice_name`` and
    ``api_parameters``. The provider identifier selects the configuration class
    and ``api_parameters`` are passed to it as keyword arguments.

    Args:
        path: File holding the voice configuration list.

    Returns:
        One configuration per entry, in file order.

    Raises:
        ConfigError: The file is missing, unreadable or invalid. Invalid covers
            content that is not a list of objects, a missing field, an unknown
            provider identifier and an invalid API parameter.
    """
    raw = load_json(str(path))
    if not isinstance(raw, list):
        raise ConfigError(f"{Path(path).name} must contain a list of voice entries")
    configs = list[TTSConfig]()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"voice entry {index} must be an object")
        try:
            provider_id = entry["provider_id"]
            voice_name = entry["voice_name"]
        except KeyError as error:
            raise ConfigError(f"voice entry {index} missing field: {error}") from error
        if not isinstance(voice_name, str) or not voice_name:
            raise ConfigError(f"voice entry {index} voice_name must be a nonempty str")
        builder = _CONFIG_BUILDERS.get(provider_id)
        if builder is None:
            raise ConfigError(
                f"voice entry {index} unsupported provider_id: {provider_id}"
            )
        api_parameters = entry.get("api_parameters", {})
        if not isinstance(api_parameters, dict):
            raise ConfigError(f"voice entry {index} api_parameters must be an object")
        configs.append(_build_config(builder, index, voice_name, api_parameters))
    return configs


def _build_config(
    builder: Callable[..., TTSConfig],
    index: int,
    voice_name: str,
    api_parameters: dict[str, Any],
) -> TTSConfig:
    try:
        return builder(voice_name, **api_parameters)
    except (TypeError, ValueError) as error:
        raise ConfigError(
            f"voice entry {index} invalid api_parameters: {error}"
        ) from error
