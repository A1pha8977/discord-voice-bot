import logging
from pathlib import Path

import discord
from discord.ext import commands

from cogs.audio_media.audio_media_cog import AudioMediaCog
from cogs.tts.tts_cog import TTSCog
from cogs.voice_session_cog import VoiceSessionCog
from services.tts.deepseek_tts_client import DeepseekTTSClient
from services.tts.fish_audio_tts_client import FishAudioTTSClient
from services.tts.tts_client import TTSClient
from services.voice_manager.voice_manager import VoiceManager
from utils.config.env_loader import load_env

_logger = logging.getLogger(__name__)


class MyBot(commands.Bot):
    def __init__(self) -> None:
        self._tree_synced = False
        self._voice_manager = VoiceManager()
        self._tts_clients = self._create_tts_clients()
        super().__init__(
            command_prefix="/",
            intents=discord.Intents.default(),
        )

    def _create_tts_clients(self) -> list[TTSClient]:
        """Create one client for every provider that has an API key.

        Raises:
            ValueError: No provider API key is configured.
        """
        tts_clients = list[TTSClient]()
        deepseek_api_key = load_env("DEEPSEEK_API_KEY")
        if deepseek_api_key is not None:
            tts_clients.append(DeepseekTTSClient(deepseek_api_key))
        fish_api_key = load_env("FISH_AUDIO_API_KEY")
        if fish_api_key is not None:
            tts_clients.append(FishAudioTTSClient(fish_api_key))
        if not tts_clients:
            raise ValueError("no tts provider api key")
        return tts_clients

    async def setup_hook(self) -> None:
        # TODO : add my cogs here
        await self.add_cog(
            AudioMediaCog(
                self._voice_manager,
                Path(__file__).resolve().parent / "config" / "sound_pad",
            )
        )
        await self.add_cog(VoiceSessionCog(self._voice_manager))

        await self.add_cog(
            TTSCog(
                self._tts_clients,
                Path(__file__).resolve().parent / "config" / "tts_config.json",
                self._voice_manager,
            )
        )
        self._voice_manager.start()
        return await super().setup_hook()

    async def on_ready(self) -> None:
        """Log the connected identity and best-effort sync global slash commands.

        Successful sync happens once per bot instance, not once per reconnect.
        Sync exceptions are logged rather than propagated, leaving the flag unset
        so a later ready event can retry. No immediate retry loop is started.
        """
        if self.user is not None:
            _logger.info(
                "Connected to Discord as %s (ID: %s)",
                self.user,
                self.user.id,
            )
        if self._tree_synced:
            return
        try:
            synced = await self.tree.sync()
            self._tree_synced = True
            _logger.info(
                "Global slash command sync completed: %s",
                [c.name for c in synced],
            )
        except Exception:
            _logger.exception("Global slash command sync failed.")

    async def close(self) -> None:
        self._voice_manager.close()
        return await super().close()
