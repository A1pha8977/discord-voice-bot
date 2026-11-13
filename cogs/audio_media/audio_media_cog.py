import logging
from itertools import islice
from pathlib import Path

import discord
from discord import FFmpegPCMAudio, Member, app_commands
from discord.ext import commands

from cogs.audio_media._internal.songs import Songs
from services.voice_manager.errors import VoiceManagerQueueFullError
from services.voice_manager.playback_item import (
    PlaybackPriority,
    PlaybackSpec,
    PlaybackStatus,
)
from services.voice_manager.voice_manager import VoiceManager

_logger = logging.getLogger(__name__)

_AUDIO_KIND = "audio"


class AudioMediaCog(commands.Cog):
    def __init__(self, voice_manager: VoiceManager, songs_dir: Path) -> None:
        self._voice_manager = voice_manager
        self._songs_dir = songs_dir
        self._songs = Songs()
        self._discover_songs()

    @app_commands.command(name="play")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, sound_name: str) -> None:
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.play.name} is only available in guild", ephemeral=True
            )
            return
        bot_voice_client = interaction.guild.voice_client
        if not bot_voice_client:
            await interaction.response.send_message(
                "The bot is not in a voice channel", ephemeral=True
            )
            return
        if sound_name not in self._songs.names():
            await interaction.response.send_message(
                f"unknown sound name: {sound_name}", ephemeral=True
            )
            return
        song_path = self._songs[sound_name]
        spec = PlaybackSpec(
            kind=_AUDIO_KIND,
            name=f"{interaction.user.display_name}: {sound_name}",
            priority=PlaybackPriority.LOW,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        audio_source: FFmpegPCMAudio | None = None
        try:
            audio_source = FFmpegPCMAudio(str(song_path))
            task = self._voice_manager.plan(
                audio_source,
                interaction,
                spec,
                finished_callback=lambda task: _logger.info(
                    "playback finished: playback=%s", task.spec.name
                ),
                failed_callback=lambda task: _logger.error(
                    "playback failed: playback=%s reason=%s",
                    task.spec.name,
                    task.failed_reason(),
                ),
            )
        except (
            VoiceManagerQueueFullError,
            discord.ClientException,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            message = "Failed to plan audio."
            if isinstance(error, VoiceManagerQueueFullError):
                message = "The audio queue is full."
            else:
                _logger.exception("Failed to plan audio: playback=%s", spec.name)
            try:
                if audio_source is not None:
                    audio_source.cleanup()
            finally:
                await interaction.edit_original_response(content=message)
            return

        if task.status() == PlaybackStatus.FAILED:
            await interaction.edit_original_response(content="Failed to plan audio.")
            return
        await interaction.edit_original_response(content="Audio planned")

    @play.autocomplete(str(play.parameters[0].name))
    async def play_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest up to 25 original names matching a case-insensitive prefix."""
        prefix = current.casefold()
        names = sorted(self._songs.names(), key=lambda name: (name.casefold(), name))
        matches = (name for name in names if name.casefold().startswith(prefix))
        return [
            app_commands.Choice(name=song_name, value=song_name)
            for song_name in islice(matches, 25)
        ]

    @app_commands.command(name="volume_song")
    @app_commands.guild_only()
    async def volume_song(
        self,
        interaction: discord.Interaction,
        volume: app_commands.Range[int, 0, 200] | None = None,
    ) -> None:
        """Show or set the volume of the local audio playback in percent."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.volume_song.name} is only available in guild", ephemeral=True
            )
            return
        if volume is None:
            current = self._voice_manager.get_volume(interaction, _AUDIO_KIND)
            await interaction.response.send_message(f"Song volume is {current:.0%}")
            return
        self._voice_manager.set_volume(interaction, _AUDIO_KIND, volume / 100)
        await interaction.response.send_message(f"Song volume set to {volume}%")

    @app_commands.command(name="songs")
    @app_commands.guild_only()
    async def songs(self, interaction: discord.Interaction) -> None:
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.songs.name} is only available in guild", ephemeral=True
            )
            return
        songs = sorted(self._songs.names())
        if not songs:
            await interaction.response.send_message(
                "no song is available", ephemeral=True
            )
        else:
            await interaction.response.send_message("\n".join(songs), ephemeral=True)

    @app_commands.command(name="reload_songs")
    @app_commands.guild_only()
    async def reload_songs(self, interaction: discord.Interaction) -> None:
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.reload_songs.name} is only available in guild", ephemeral=True
            )
            return
        try:
            self._discover_songs()
        except OSError:
            _logger.exception("Failed to reload songs")
            await interaction.response.send_message(
                "Failed to reload songs.", ephemeral=True
            )
            return
        await interaction.response.send_message("songs reloaded", ephemeral=True)

    def _discover_songs(self) -> None:
        """Replace the catalog after scanning the current readable song files."""
        songs = Songs()
        for file in self._songs_dir.iterdir():
            if file.is_file():
                try:
                    with open(file, "rb") as _:
                        songs[file.stem] = file
                        _logger.info(f"added sound: {file}")
                except OSError:
                    _logger.error(f"can not open file: {file}")
            else:
                _logger.warning(f"unexpected file type: {file}")
        self._songs = songs
