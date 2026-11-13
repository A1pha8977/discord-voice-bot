from asyncio import get_running_loop
from collections.abc import Callable

from discord import AudioSource, Interaction

from services.voice_manager._internal.guild_playback import GuildPlayback
from services.voice_manager._internal.playback_audio_source import PlaybackAudioSource
from services.voice_manager.errors import VoiceManagerQueueFullError
from services.voice_manager.playback_item import (
    PlaybackHandle,
    PlaybackItem,
    PlaybackSpec,
)


class VoiceManager:
    """Route playback requests to the state of each guild.

    The manager owns one ``GuildPlayback`` per guild and creates the playback
    item for each request. All methods run on the owning event loop thread.
    """

    def __init__(self, *, default_volume: float = 1.0) -> None:
        """Create an empty manager.

        Args:
            default_volume: Volume multiplier used by a guild playback when it
                is created. ``0.0`` is silence and ``1.0`` keeps the source
                level.
        """
        self._guild_playbacks = dict[int, GuildPlayback]()
        self._default_volume = default_volume
        self.closed = False

    def plan(
        self,
        audio: AudioSource,
        interaction: Interaction,
        spec: PlaybackSpec,
        *,
        before_play_callback: Callable[[PlaybackHandle], None] | None = None,
        finished_callback: Callable[[PlaybackHandle], None] | None = None,
        failed_callback: Callable[[PlaybackHandle], None] | None = None,
    ) -> PlaybackHandle:
        """Create and enqueue a playback item on the owning event loop.

        Args:
            audio: Source to play. Ownership transfers to the returned item once
                it is queued.
            interaction: Interaction whose guild and voice client receive the
                playback.
            spec: Kind and priority described by the submitter of the playback.
                A higher priority item plays before queued lower priority items
                and interrupts the current one.
            before_play_callback: Called on the owning loop right before the
                source is submitted to the voice client.
            finished_callback: Called when playback finishes normally.
            failed_callback: Called when playback fails or is rejected.

        Returns:
            The created item. Inspect ``status()`` to learn whether it was
            accepted by the guild queue.

        Raises:
            RuntimeError: The manager is already closed.
            ValueError: The bot is not connected to a voice channel.
            VoiceManagerQueueFullError: The guild queue is full. The caller
                retains ownership of the audio source and its cleanup.
        """
        if self.closed:
            raise RuntimeError("voice manager is closed")
        if (
            interaction.guild is None
            or interaction.guild.voice_client is None
            or interaction.guild.voice_client.channel is None
        ):
            raise ValueError("the bot must in a voice channel")
        guild_id = interaction.guild.id
        guild_playback = self._guild_playbacks.get(guild_id)
        if guild_playback is not None and guild_playback.is_full():
            raise VoiceManagerQueueFullError()
        task = PlaybackItem(
            PlaybackAudioSource(audio, get_running_loop()),
            interaction,
            spec,
            before_play_callback=before_play_callback,
            finished_callback=finished_callback,
            failed_callback=failed_callback,
        )
        if guild_playback is None:
            guild_playback = self._create_guild_playback(guild_id)
        guild_playback.plan(task)
        return task

    def _create_guild_playback(self, guild_id: int) -> GuildPlayback:
        """Create and start the guild playback for ``guild_id``.

        The worker is started right away, so a later ``plan()`` and a volume set
        before the first playback both reach a running playback.

        Raises:
            RuntimeError: The manager is already closed.
        """
        guild_playback = GuildPlayback(guild_id, default_volume=self._default_volume)
        self._guild_playbacks[guild_id] = guild_playback
        guild_playback.start()
        return guild_playback

    def is_playing(self, interaction: Interaction) -> bool:
        """Report whether the guild of ``interaction`` is playing audio.

        Args:
            interaction: Interaction whose guild is queried.

        Returns:
            True when an item is playing. False when the interaction is outside
            a guild, the guild has no playback, or the current item is paused.
        """
        if interaction.guild is None:
            return False
        guild_playback = self._guild_playbacks.get(interaction.guild.id)
        return guild_playback is not None and guild_playback.is_playing()

    def is_paused(self, interaction: Interaction) -> bool:
        """Report whether the guild of ``interaction`` has a paused item.

        Args:
            interaction: Interaction whose guild is queried.

        Returns:
            True when the current item is paused. False when the interaction is
            outside a guild, the guild has no playback, or nothing is paused.
        """
        if interaction.guild is None:
            return False
        guild_playback = self._guild_playbacks.get(interaction.guild.id)
        return guild_playback is not None and guild_playback.is_paused()

    def pause(self, interaction: Interaction) -> bool:
        """Pause the item playing in the guild of ``interaction``.

        Args:
            interaction: Interaction whose guild is paused.

        Returns:
            True when the item moved to ``PAUSED``. False when the guild has no
            playback or nothing is playing.

        Raises:
            RuntimeError: The manager is closed; ``False`` is returned instead
                when the manager is open and the guild is idle.
            ValueError: The bot is not connected to a voice channel.
        """
        if self.closed:
            raise RuntimeError("voice manager is closed")
        if (
            interaction.guild is None
            or interaction.guild.voice_client is None
            or interaction.guild.voice_client.channel is None
        ):
            raise ValueError("the bot must in a voice channel")
        guild_playback = self._guild_playbacks.get(interaction.guild.id)
        if guild_playback is None:
            return False
        return guild_playback.pause()

    def resume(self, interaction: Interaction) -> bool:
        """Resume the paused item in the guild of ``interaction``.

        Args:
            interaction: Interaction whose guild is resumed.

        Returns:
            True when the item moved back to ``PLAYING``. False when the guild
            has no playback or nothing is paused.

        Raises:
            RuntimeError: The manager is closed; ``False`` is returned instead
                when the manager is open and nothing is paused.
            ValueError: The bot is not connected to a voice channel.
        """
        if self.closed:
            raise RuntimeError("voice manager is closed")
        if (
            interaction.guild is None
            or interaction.guild.voice_client is None
            or interaction.guild.voice_client.channel is None
        ):
            raise ValueError("the bot must in a voice channel")
        guild_playback = self._guild_playbacks.get(interaction.guild.id)
        if guild_playback is None:
            return False
        return guild_playback.resume()

    def cancel(self, interaction: Interaction) -> bool:
        """Cancel the item playing or paused in the guild of ``interaction``.

        Args:
            interaction: Interaction whose guild is cancelled.

        Returns:
            True when an item was cancelled. False when the guild has no
            playback or nothing is playing.

        Raises:
            RuntimeError: The voice manager or the guild playback is closed.
            ValueError: The bot is not connected to a voice channel.
        """
        if self.closed:
            raise RuntimeError("voice manager is closed")
        if (
            interaction.guild is None
            or interaction.guild.voice_client is None
            or interaction.guild.voice_client.channel is None
        ):
            raise ValueError("the bot must in a voice channel")
        guild_playback = self._guild_playbacks.get(interaction.guild.id)
        if guild_playback is None:
            return False
        return guild_playback.cancel()

    def start(self) -> None:
        """Keep the startup hook without global work.

        Each guild playback starts lazily on its first request, so this call
        does nothing yet.

        Raises:
            RuntimeError: The manager is already closed.
        """
        if self.closed:
            raise RuntimeError("voice manager is closed")

    def close(self) -> None:
        """Request shutdown of every guild playback.

        The manager stays closed afterwards: ``plan()``, ``start()``,
        ``pause()`` and ``resume()`` raise ``RuntimeError`` while ``is_playing``
        and ``is_paused`` report ``False``. Repeated calls do nothing.

        Playback items that have not started are cancelled and the workers stop
        once they return to their loop.
        """
        if self.closed:
            return
        self.closed = True
        for guild_playback in self._guild_playbacks.values():
            guild_playback.close()

    def get_volume(self, interaction: Interaction, kind: str) -> float:
        """Return the volume of one kind in the guild of ``interaction``.

        Args:
            interaction: Interaction whose guild is queried.
            kind: Group name of the playback, for example ``tts``.

        Returns:
            The volume stored for the kind in the guild playback, or
            ``default_volume`` of the manager when the guild has no playback yet.

        Raises:
            RuntimeError: The manager is closed.
            ValueError: The bot is not in a guild, or the kind is empty or
                contains whitespace only.
        """
        if self.closed:
            raise RuntimeError("voice manager is closed")
        if interaction.guild is None:
            raise ValueError("the bot must in a guild")
        if not kind.strip():
            raise ValueError(f"kind must not be empty, got {kind!r}")
        guild_playback = self._guild_playbacks.get(interaction.guild.id)
        if guild_playback is None:
            return self._default_volume
        return guild_playback.get_volume(kind)

    def set_volume(self, interaction: Interaction, kind: str, volume: float) -> None:
        """Set the volume of one kind in the guild of ``interaction``.

        The value applies to the item that is playing and to the items planned
        afterwards with the same kind; other kinds and other guilds keep their
        own volume. The guild playback is created when the guild has none yet,
        so a volume can be set before the first playback.

        Args:
            interaction: Interaction whose guild receives the volume.
            kind: Group name of the playback, for example ``tts``.
            volume: ``0.0`` is silence and ``1.0`` keeps the source level.

        Raises:
            RuntimeError: The manager is closed.
            ValueError: The volume is outside ``0.0`` to ``2.0``, the kind is
                empty or contains whitespace only, or the bot is not in a guild.
        """
        if self.closed:
            raise RuntimeError("voice manager is closed")
        if interaction.guild is None:
            raise ValueError("the bot must in a guild")
        if not kind.strip():
            raise ValueError(f"kind must not be empty, got {kind!r}")
        if not 0.0 <= volume <= 2.0:
            raise ValueError(f"volume must be between 0.0 and 2.0, got {volume}")
        guild_playback = self._guild_playbacks.get(interaction.guild.id)
        if guild_playback is None:
            guild_playback = self._create_guild_playback(interaction.guild.id)
        guild_playback.set_volume(kind, volume)
