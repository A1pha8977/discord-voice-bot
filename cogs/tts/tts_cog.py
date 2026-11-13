import logging
from itertools import islice
from pathlib import Path

import discord
from discord import Member, app_commands
from discord.ext import commands

from cogs.tts._internal.audio_reader_source import PCMAudioReaderSource
from services.tts.tts_client import TTSClient
from services.tts.tts_config import TTSConfig
from services.tts.tts_config_loader import load_tts_configs
from services.voice_manager.errors import VoiceManagerQueueFullError
from services.voice_manager.playback_item import (
    PlaybackPriority,
    PlaybackSpec,
    PlaybackStatus,
)
from services.voice_manager.voice_manager import VoiceManager
from utils.audio.audio_format import AudioFormatType
from utils.config.json_loader import ConfigError

_logger = logging.getLogger(__name__)

_TTS_KIND = "tts"


class TTSCog(commands.Cog):
    def __init__(
        self,
        tts_clients: list[TTSClient],
        tts_config_path: Path,
        voice_manager: VoiceManager,
    ) -> None:
        """Create the cog that plays synthesized speech on request.

        Args:
            tts_clients: Clients used to synthesize speech, one per provider.
            tts_config_path: File listing the available voices. The first
                usable entry is the default for users without a selection.
            voice_manager: Queue that plays the produced audio in voice channels.

        Raises:
            ValueError: No client is available or two clients serve the same
                provider.
            ConfigError: The voice file is unreadable or invalid, lists no
                voice of an available provider, or repeats a voice name.
        """
        if not tts_clients:
            raise ValueError(f"{type(self).__name__} requires at least one tts client")
        provider_to_tts_client = {
            tts_client.tts_provider_id(): tts_client for tts_client in tts_clients
        }
        if len(provider_to_tts_client) != len(tts_clients):
            raise ValueError(f"{type(self).__name__} duplicated tts client provider id")
        name_to_tts_config = self._load_name_to_tts_config(
            tts_config_path, provider_to_tts_client
        )
        self._tts_config_path = tts_config_path
        self._voice_manager = voice_manager
        self._provider_to_tts_client = provider_to_tts_client
        self._name_to_tts_config = name_to_tts_config
        self._default_tts_config = next(iter(name_to_tts_config.values()))
        self._user_tts_config: dict[int, TTSConfig] = {}
        super().__init__()

    @app_commands.command(name="tts")
    @app_commands.guild_only()
    async def tts(self, interaction: discord.Interaction, text: str) -> None:
        """Synthesize ``text`` and play the audio in the bot's voice channel."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.tts.name} is only available in guild", ephemeral=True
            )
            return
        bot_voice_client = interaction.guild.voice_client
        if not bot_voice_client:
            await interaction.response.send_message(
                "The bot is not in a voice channel", ephemeral=True
            )
            return
        guild_name = interaction.guild.name
        config = self._user_tts_config.get(
            interaction.user.id, self._default_tts_config
        )
        spec = PlaybackSpec(
            kind=_TTS_KIND,
            name=f"{interaction.user.display_name}: {config.voice_name()}",
            priority=PlaybackPriority.HIGH,
        )
        await interaction.response.defer(ephemeral=True)
        tts_client = self._provider_to_tts_client.get(config.tts_provider_id())
        if tts_client is None:
            _logger.error(
                "TTS provider has no client: guild=%s playback=%s provider=%s",
                guild_name,
                spec.name,
                config.tts_provider_id(),
            )
            await interaction.edit_original_response(
                content="Failed to generate speech."
            )
            return
        try:
            audio_reader = tts_client.generate(text, config)
        except Exception:
            _logger.exception(
                "TTS generation failed: guild=%s playback=%s",
                guild_name,
                spec.name,
            )
            await interaction.edit_original_response(
                content="Failed to generate speech."
            )
            return

        audio_format = audio_reader.format()
        if audio_format.type != AudioFormatType.PCM:
            _logger.error(
                "TTS voice produced an unsupported format: guild=%s playback=%s format=%s",
                guild_name,
                spec.name,
                audio_format.type.value,
            )
            await audio_reader.read_close()
            await interaction.edit_original_response(
                content=f"unsupported audio format: {audio_format.type.value}"
            )
            return
        source: PCMAudioReaderSource | None = None
        try:
            source = PCMAudioReaderSource(audio_reader)
            task = self._voice_manager.plan(
                source,
                interaction,
                spec,
                before_play_callback=lambda task: _logger.info(
                    "TTS playback starting: guild=%s playback=%s",
                    guild_name,
                    task.spec.name,
                ),
                finished_callback=lambda task: _logger.info(
                    "TTS playback finished: guild=%s playback=%s status=%s",
                    guild_name,
                    task.spec.name,
                    task.status(),
                ),
                failed_callback=lambda task: _logger.error(
                    "TTS playback failed: guild=%s playback=%s reason_type=%s",
                    guild_name,
                    task.spec.name,
                    type(task.failed_reason()).__name__,
                ),
            )
        except Exception as error:
            if source is not None:
                source.cleanup()
            else:
                await audio_reader.read_close()
            if isinstance(error, VoiceManagerQueueFullError):
                message = "The audio queue is full."
            else:
                message = "Failed to play speech."
                _logger.exception(
                    "TTS playback planning failed: guild=%s playback=%s",
                    guild_name,
                    spec.name,
                )
            await interaction.edit_original_response(content=message)
            return

        if task.status() == PlaybackStatus.FAILED:
            await interaction.edit_original_response(content="Failed to play speech.")
            return
        _logger.info(
            "TTS playback submitted: guild=%s playback=%s status=%s",
            guild_name,
            task.spec.name,
            task.status(),
        )
        await interaction.delete_original_response()

    @app_commands.command(name="volume_tts")
    @app_commands.guild_only()
    async def volume_tts(
        self,
        interaction: discord.Interaction,
        volume: app_commands.Range[int, 0, 200] | None = None,
    ) -> None:
        """Show or set the volume of the speech playback in percent."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.volume_tts.name} is only available in guild", ephemeral=True
            )
            return
        if volume is None:
            current = self._voice_manager.get_volume(interaction, _TTS_KIND)
            await interaction.response.send_message(f"TTS volume is {current:.0%}")
            return
        self._voice_manager.set_volume(interaction, _TTS_KIND, volume / 100)
        await interaction.response.send_message(f"TTS volume set to {volume}%")

    @app_commands.command(name="voice")
    @app_commands.guild_only()
    async def voice(self, interaction: discord.Interaction, voice_name: str) -> None:
        """Select the voice configuration used for later ``/tts`` requests."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.voice.name} is only available in guild", ephemeral=True
            )
            return
        config = self._name_to_tts_config.get(voice_name)
        if config is None:
            await interaction.response.send_message(
                f"unknown voice name: {voice_name}", ephemeral=True
            )
            return
        self._user_tts_config[interaction.user.id] = config
        _logger.info(
            "TTS voice selected: guild=%s user=%s voice=%s",
            interaction.guild_id,
            interaction.user.id,
            voice_name,
        )
        await interaction.response.send_message(
            f"voice set to {voice_name}", ephemeral=True
        )

    @voice.autocomplete(str(voice.parameters[0].name))
    async def voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest up to 25 voice names matching a case-insensitive prefix."""
        prefix = current.casefold()
        names = sorted(
            self._name_to_tts_config, key=lambda name: (name.casefold(), name)
        )
        matches = (name for name in names if name.casefold().startswith(prefix))
        return [
            app_commands.Choice(name=name, value=name) for name in islice(matches, 25)
        ]

    @app_commands.command(name="voices")
    @app_commands.guild_only()
    async def voices(self, interaction: discord.Interaction) -> None:
        """List the available voice names and mark the caller's voice."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.voices.name} is only available in guild", ephemeral=True
            )
            return
        selected = self._user_tts_config.get(
            interaction.user.id, self._default_tts_config
        )
        names = sorted(
            self._name_to_tts_config, key=lambda name: (name.casefold(), name)
        )
        lines = [
            f"{name} (selected)" if self._name_to_tts_config[name] is selected else name
            for name in names
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="reload_voices")
    @app_commands.guild_only()
    async def reload_voices(self, interaction: discord.Interaction) -> None:
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.reload_voices.name} is only available in guild", ephemeral=True
            )
            return
        try:
            name_to_tts_config = self._load_name_to_tts_config(
                self._tts_config_path, self._provider_to_tts_client
            )
        except ConfigError:
            _logger.exception("Failed to reload voices")
            await interaction.response.send_message(
                "Failed to reload voices.", ephemeral=True
            )
            return
        self._name_to_tts_config = name_to_tts_config
        self._default_tts_config = next(iter(name_to_tts_config.values()))
        self._user_tts_config = {
            user_id: name_to_tts_config[tts_config.voice_name()]
            for user_id, tts_config in self._user_tts_config.items()
            if tts_config.voice_name() in name_to_tts_config
        }
        _logger.info("TTS voices reloaded: count=%s", len(name_to_tts_config))
        await interaction.response.send_message("voices reloaded", ephemeral=True)

    @staticmethod
    def _load_name_to_tts_config(
        tts_config_path: Path,
        provider_to_tts_client: dict[str, TTSClient],
    ) -> dict[str, TTSConfig]:
        """Load the voices served by one of ``provider_to_tts_client``.

        Args:
            tts_config_path: File listing the available voices.
            provider_to_tts_client: Clients keyed by provider identifier.

        Returns:
            Voice configurations keyed by voice name, in file order.

        Raises:
            ConfigError: The file is unreadable or invalid, no entry uses an
                available provider, or two entries share the same voice name.
        """
        tts_configs = [
            tts_config
            for tts_config in load_tts_configs(tts_config_path)
            if tts_config.tts_provider_id() in provider_to_tts_client
        ]
        if not tts_configs:
            raise ConfigError("no tts config matches an available tts client")
        name_to_tts_config = {
            tts_config.voice_name(): tts_config for tts_config in tts_configs
        }
        if len(name_to_tts_config) != len(tts_configs):
            raise ConfigError("duplicated tts config voice name")
        return name_to_tts_config
