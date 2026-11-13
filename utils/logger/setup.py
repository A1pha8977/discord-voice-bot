import logging
from collections.abc import Mapping
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(
    log_dir: Path | None = None,
    *,
    level: int = logging.INFO,
    module_levels: Mapping[str, int] | None = None,
) -> None:
    """Replace root logging handlers with console and rotating file output.

    Args:
        log_dir: Directory for bot.log, created recursively when absent. None
            selects the project's log directory, independent of the current cwd.
        level: Level for the root logger and its handlers.
        module_levels: Optional per-module levels keyed by logger name. Each
            entry overrides the level of that logger, for example
            ``{"services.tts.fish_audio_tts_client": logging.DEBUG}``.

    Raises:
        OSError: Directory creation or opening the log file fails, including
            permission errors or an existing non-directory path.

    Existing root handlers are removed and closed by basicConfig(force=True).
    Rotation uses local midnight, occurs on the next emitted record, and keeps
    all archives. Application startup owns configuration; modules should use
    standard named loggers instead of calling this function themselves.
    """
    if log_dir is None:
        log_dir = Path(__file__).resolve().parents[2] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        log_dir / "bot.log",
        when="midnight",
        interval=1,
        backupCount=0,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        handlers=[logging.StreamHandler(), file_handler],
        force=True,
    )
    for name, module_level in (module_levels or {}).items():
        logging.getLogger(name).setLevel(module_level)
