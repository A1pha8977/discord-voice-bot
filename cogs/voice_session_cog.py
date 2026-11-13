import logging

import discord
from discord import Member, VoiceClient, app_commands
from discord.ext import commands

from services.voice_manager.voice_manager import VoiceManager

_logger = logging.getLogger(__name__)


class VoiceSessionCog(commands.Cog):
    """Manage the bot voice session in a guild."""

    async def cog_load(self) -> None:
        return await super().cog_load()

    def __init__(self, voice_manager: VoiceManager) -> None:
        self._voice_manager = voice_manager

    @app_commands.command(name="connect")
    @app_commands.guild_only()
    async def connect(self, interaction: discord.Interaction) -> None:
        """Join the voice channel of the caller, or move the bot into it."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.connect.name} is only available in guild", ephemeral=True
            )
            return
        member = interaction.user
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                f"You should join a voice channel before using /{self.connect.name}",
                ephemeral=True,
            )
            return

        voice_channel = member.voice.channel
        bot_voice_client = interaction.guild.voice_client
        await interaction.response.defer(ephemeral=True)
        if not isinstance(bot_voice_client, VoiceClient):
            await voice_channel.connect()
            action = "joined"
        else:
            await bot_voice_client.move_to(voice_channel)
            action = "moved"
        _logger.info(
            "Voice channel %s: guild=%s channel=%s",
            action,
            interaction.guild.name,
            voice_channel.name,
        )
        await interaction.delete_original_response()

    @app_commands.command(name="quit")
    @app_commands.guild_only()
    async def quit(self, interaction: discord.Interaction) -> None:
        """Leave the voice channel the bot is connected to."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.quit.name} is only available in guild", ephemeral=True
            )
            return
        bot_voice_client = interaction.guild.voice_client
        bot_voice_channel = bot_voice_client.channel if bot_voice_client else None
        if not bot_voice_client or not bot_voice_channel:
            await interaction.response.send_message(
                "The bot is not in a voice channel", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await bot_voice_client.disconnect(force=False)
        _logger.info(
            "Voice channel left: guild=%s channel=%s",
            interaction.guild.name,
            getattr(bot_voice_channel, "name", None),
        )
        await interaction.delete_original_response()

    @app_commands.command(name="pause")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        """Pause the audio playing in the bot voice channel."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.pause.name} is only available in guild", ephemeral=True
            )
            return
        if self._voice_manager.is_paused(interaction):
            await interaction.response.send_message("Already paused", ephemeral=True)
            return
        if not self._voice_manager.is_playing(interaction):
            await interaction.response.send_message(
                "Nothing is playing", ephemeral=True
            )
            return
        paused = self._voice_manager.pause(interaction)
        if not paused:
            await interaction.response.send_message(
                "Pause failed, please try again", ephemeral=True
            )
            return
        _logger.info(
            "Audio paused: guild=%s channel=%s",
            interaction.guild.name,
            getattr(
                getattr(interaction.guild.voice_client, "channel", None), "name", None
            ),
        )
        await interaction.response.send_message("Audio paused")

    @app_commands.command(name="resume")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        """Resume the paused audio in the bot voice channel."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.resume.name} is only available in guild", ephemeral=True
            )
            return
        if not self._voice_manager.is_paused(interaction):
            await interaction.response.send_message("Nothing is paused", ephemeral=True)
            return
        resumed = self._voice_manager.resume(interaction)
        if not resumed:
            await interaction.response.send_message(
                "Resume failed, please try again", ephemeral=True
            )
            return
        _logger.info(
            "Audio resumed: guild=%s channel=%s",
            interaction.guild.name,
            getattr(
                getattr(interaction.guild.voice_client, "channel", None), "name", None
            ),
        )
        await interaction.response.send_message("Audio resumed")

    @app_commands.command(name="next")
    @app_commands.guild_only()
    async def next(self, interaction: discord.Interaction) -> None:
        """Skip the current audio and start the next queued one."""
        if (
            not interaction.channel
            or not isinstance(interaction.user, Member)
            or not interaction.guild
        ):
            await interaction.response.send_message(
                f"/{self.next.name} is only available in guild", ephemeral=True
            )
            return
        if not self._voice_manager.is_playing(
            interaction
        ) and not self._voice_manager.is_paused(interaction):
            await interaction.response.send_message(
                "Nothing is playing", ephemeral=True
            )
            return
        skipped = self._voice_manager.cancel(interaction)
        if not skipped:
            await interaction.response.send_message(
                "Skip failed, please try again", ephemeral=True
            )
            return
        _logger.info(
            "Audio skipped: guild=%s channel=%s",
            interaction.guild.name,
            getattr(
                getattr(interaction.guild.voice_client, "channel", None), "name", None
            ),
        )
        await interaction.response.send_message("Skipped to the next audio")

    # @app_commands.command(name="volume")
    # @app_commands.guild_only()
    # async def volume(
    #     self,
    #     interaction: discord.Interaction,
    #     volume: app_commands.Range[int, 0, 200] | None = None,
    # ) -> None:
    #     """Show or set the playback volume in percent."""
    #     if (
    #         not interaction.channel
    #         or not isinstance(interaction.user, Member)
    #         or not interaction.guild
    #     ):
    #         await interaction.response.send_message(
    #             f"/{self.volume.name} is only available in guild", ephemeral=True
    #         )
    #         return
    #     if volume is None:
    #         current = self._voice_manager.get_volume(interaction)
    #         await interaction.response.send_message(f"Current volume is {current:.0%}")
    #         return
    #     if not self._voice_manager.set_volume(interaction, volume / 100):
    #         await interaction.response.send_message(
    #             "Nothing is playing", ephemeral=True
    #         )
    #         return
    #     await interaction.response.send_message(f"Volume set to {volume}%")
