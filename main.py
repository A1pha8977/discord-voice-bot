import argparse
import logging

from bot import MyBot
from utils import logger
from utils.config.env_loader import load_env

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Discord voice bot.")
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default="INFO",
        help="root log level, defaults to INFO",
    )
    parser.add_argument(
        "--debug-module",
        action="append",
        default=[],
        metavar="NAME",
        help="logger name to set to DEBUG, repeatable",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logger.setup_logger(
        level=_LOG_LEVELS[args.log_level],
        module_levels={name: logging.DEBUG for name in args.debug_module},
    )
    bot_token = load_env("DISCORD_BOT_TOKEN")
    if bot_token is None:
        raise ValueError("no discord bot token")
    bot = MyBot()
    bot.run(bot_token)


if __name__ == "__main__":
    main()
