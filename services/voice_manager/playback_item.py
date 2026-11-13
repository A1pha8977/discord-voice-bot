"""Playback item and the enums that describe its priority and state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum, auto
from typing import Protocol

from discord import VoiceClient
from discord.interactions import Interaction

from services.voice_manager._internal.playback_audio_source import (
    PlaybackAudioSource,
    ReadyCallback,
)
from services.voice_manager.errors import VoiceChannelDisconnected, VoiceClientError


class PlaybackPriority(IntEnum):
    """Priority of a playback item.

    Lower values play first. A new item interrupts the current one when its
    priority is lower than the priority of the current item, and the interrupted
    item is queued again so it resumes before the other items of its own
    priority.
    """

    HIGH = 0
    NORMAL = 1
    LOW = 2


@dataclass(frozen=True)
class PlaybackSpec:
    """Description of one playback, filled in by the submitter.

    Attributes:
        kind: Group of the playback, for example ``tts`` or ``audio``. Shared by
            many playbacks and compared verbatim, so a submitter must stay
            consistent.
        name: Name of this single playback, meant for logs and messages, for
            example the requester together with the audio or voice name.
            Unrelated to ``kind``.
        priority: Ordering of the item in the guild queue.

    Raises:
        ValueError: ``kind`` or ``name`` is empty or contains only whitespace.
    """

    kind: str
    name: str
    priority: PlaybackPriority

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError(f"kind must not be empty, got {self.kind!r}")
        if not self.name.strip():
            raise ValueError(f"name must not be empty, got {self.name!r}")


class PlaybackStatus(Enum):
    """State of a playback item.

    ``CANCELLED``, ``FAILED`` and ``FINISHED`` are terminal and release the
    source. ``WAITING`` is queued, ``PLAYING`` and ``PAUSED`` are controlled by
    the caller, and ``INTERRUPTED`` is preempted and waiting for its replay.
    """

    CANCELLED = auto()
    FAILED = auto()
    FINISHED = auto()
    WAITING = auto()
    PLAYING = auto()
    PAUSED = auto()
    INTERRUPTED = auto()


class PlaybackHandle(Protocol):
    """The playback API exposed to callers of ``VoiceManager.plan()``.

    Callers inspect and cancel a playback; the state machine methods of
    ``PlaybackItem`` stay out of this protocol on purpose.
    """

    spec: PlaybackSpec

    def cancel(self) -> None:
        """Cancel the playback."""
        ...

    def status(self) -> PlaybackStatus:
        """Return the current playback status."""
        ...

    def failed_reason(self) -> BaseException | None:
        """Return the recorded failure, or None when no failure is recorded."""
        ...


class PlaybackItem(PlaybackHandle):
    """One queued playback with synchronous state access.

    The item starts as ``WAITING``. ``play`` submits the source to the voice
    client, ``pause`` and ``resume`` control it, ``interrupt`` stops it and keeps
    the source for the next ``play`` call, and ``cancel``, ``fail`` and ``finish``
    end it. A terminal state releases the source exactly once.

    Callers receive the item as ``PlaybackHandle``; the remaining methods drive
    the state machine and belong to ``GuildPlayback``. State is read and changed
    on the event loop thread that owns the item, while ``read`` runs on the
    Discord player thread.
    """

    def __init__(
        self,
        audio: PlaybackAudioSource,
        interaction: Interaction,
        spec: PlaybackSpec,
        *,
        before_play_callback: Callable[[PlaybackHandle], None] | None = None,
        failed_callback: Callable[[PlaybackHandle], None] | None = None,
        finished_callback: Callable[[PlaybackHandle], None] | None = None,
    ) -> None:
        """Bind one source to the guild and voice channel of ``interaction``.

        Args:
            audio: Source owned by the item until a terminal state releases it.
            interaction: Interaction whose guild and voice client play the
                audio. The channel joined at construction is remembered and
                checked again on every ``play``.
            spec: Kind and priority described by the submitter of the playback.
            before_play_callback: Called on the owning loop right before the
                source is submitted, once per item.
            failed_callback: Called when the item enters ``FAILED``.
            finished_callback: Called when the item enters ``FINISHED``.

        Raises:
            ValueError: The interaction has no guild or the guild has no voice
                client.
        """
        if interaction.guild is None:
            raise ValueError("guild must not be none")
        if interaction.guild.voice_client is None:
            raise ValueError("voice_client must not be none")

        self._audio = audio
        self._interaction = interaction
        self.spec = spec
        self._guild_id = interaction.guild.id
        self._voice_channel = interaction.guild.voice_client.channel
        self._status = PlaybackStatus.WAITING
        self._failed_reason: BaseException | None = None
        self._voice_client: VoiceClient | None = None
        self._cleaned = False
        self._before_play: Callable[[PlaybackHandle], None] = (
            before_play_callback if before_play_callback is not None else lambda _: None
        )
        self._on_failed: Callable[[PlaybackHandle], None] = (
            failed_callback if failed_callback is not None else lambda _: None
        )
        self._on_finished: Callable[[PlaybackHandle], None] = (
            finished_callback if finished_callback is not None else lambda _: None
        )

    def cancel(self) -> None:
        """Cancel the playback and request it to stop on the owning loop."""
        if self._status in {
            PlaybackStatus.CANCELLED,
            PlaybackStatus.FAILED,
            PlaybackStatus.FINISHED,
        }:
            return
        self._status = PlaybackStatus.CANCELLED
        if self._voice_client is not None and self._voice_client.source is self._audio:
            self._voice_client.stop()
        self._cleanup()

    def status(self) -> PlaybackStatus:
        """Return the current playback status."""
        return self._status

    def failed_reason(self) -> BaseException | None:
        """Return the recorded failure, or None when no failure is recorded."""
        return self._failed_reason

    def fail(self, reason: BaseException) -> None:
        """Mark the item as failed and release its source.

        Raises:
            RuntimeError: The item already reached a terminal state.
        """
        if self._status in {
            PlaybackStatus.CANCELLED,
            PlaybackStatus.FAILED,
            PlaybackStatus.FINISHED,
        }:
            raise RuntimeError(
                f"fail is only supported when audio is not terminal got {self._status}"
            )
        self._status = PlaybackStatus.FAILED
        self._failed_reason = reason
        try:
            self._on_failed(self)
        finally:
            self._cleanup()

    def finish(self) -> None:
        """Mark the item as finished and release its source.

        Raises:
            RuntimeError: The item was interrupted or already reached a terminal
                state.
        """
        if self._status in {
            PlaybackStatus.CANCELLED,
            PlaybackStatus.FAILED,
            PlaybackStatus.FINISHED,
            PlaybackStatus.INTERRUPTED,
        }:
            raise RuntimeError(
                f"finish is only supported when audio is playing got {self._status}"
            )
        self._status = PlaybackStatus.FINISHED
        try:
            self._on_finished(self)
        finally:
            self._cleanup()

    def play(self) -> None:
        """Start playback and return after submitting the source to the client.

        A waiting item runs ``before_play_callback`` once, before the source is
        submitted. An interrupted item continues through the same upstream
        source without running the callback again. The source must be ready, so
        the player never starts on an upstream without audio.

        Raises:
            RuntimeError: The source is not ready, or the item is neither waiting
                nor interrupted.
        """
        if not self.is_ready():
            raise RuntimeError("the source is not ready for playback")
        if self._status not in {PlaybackStatus.WAITING, PlaybackStatus.INTERRUPTED}:
            raise RuntimeError(
                f"play is only supported when audio is waiting or interrupted got {self._status}"
            )

        assert self._interaction.guild is not None
        voice_client = self._interaction.guild.voice_client
        if voice_client is None or voice_client.channel != self._voice_channel:
            self.fail(VoiceChannelDisconnected())
            return
        if not isinstance(voice_client, VoiceClient):
            self.fail(
                TypeError("expected interaction.guild.voice_client is VoiceClient")
            )
            return

        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._voice_client_after, error)

        if self._status == PlaybackStatus.WAITING:
            self._before_play(self)
            if self._status == PlaybackStatus.CANCELLED:
                return
        self._audio.resume_reading()
        voice_client.play(self._audio, after=after)
        self._voice_client = voice_client
        self._status = PlaybackStatus.PLAYING
        return

    def pause(self) -> None:
        """Pause the submitted source and mark the playback as paused.

        Raises:
            RuntimeError: The item is not currently playing.
        """
        if self._status != PlaybackStatus.PLAYING:
            raise RuntimeError("pause is only supported when audio playing")
        assert self._voice_client is not None
        self._voice_client.pause()
        self._status = PlaybackStatus.PAUSED

    def resume(self) -> None:
        """Resume a paused source and mark the playback as playing.

        Raises:
            RuntimeError: The item is not currently paused.
        """
        if self._status != PlaybackStatus.PAUSED:
            raise RuntimeError("resume is only supported when audio is paused")
        assert self._voice_client is not None
        self._voice_client.resume()
        self._status = PlaybackStatus.PLAYING

    def interrupt(self) -> None:
        """Stop the player and keep the upstream source for a later replay.

        Raises:
            RuntimeError: The item is not playing or paused.
        """
        if self._status not in {PlaybackStatus.PLAYING, PlaybackStatus.PAUSED}:
            raise RuntimeError(
                "interrupt is only supported when audio is playing or paused"
            )
        assert self._voice_client is not None
        self._audio.stop_reading()
        self._voice_client.stop()
        self._status = PlaybackStatus.INTERRUPTED

    def get_volume(self) -> float:
        """Return the volume multiplier of this item."""
        return self._audio.get_volume()

    def set_volume(self, volume: float) -> None:
        """Set the volume of this item, including while it plays.

        Args:
            volume: ``0.0`` is silence and ``1.0`` keeps the source level.

        Raises:
            ValueError: The volume is outside ``0.0`` to ``2.0``.
        """
        self._audio.set_volume(volume)

    def preload(self) -> None:
        """Ask the source to read the first chunk ahead of the playback.

        ``is_ready`` and ``set_ready_callback`` only work after this call, since
        it is the only thing that starts the prefetch.
        """
        self._audio.preload()

    def is_ready(self) -> bool:
        """Report whether the source has audio buffered for the player.

        Stays False until ``preload`` started the prefetch.
        """
        return self._audio.is_ready()

    def set_ready_callback(self, callback: ReadyCallback | None) -> None:
        """Register the one-shot callback fired when the source becomes ready.

        The prefetch must be started with ``preload``, otherwise the callback
        never fires.

        Args:
            callback: Callback to notify with None on success or with the
                prefetch error, or None to drop the registration.
        """
        self._audio.set_ready_callback(callback)

    def _cleanup(self) -> None:
        """Stop the reads and release the upstream source once."""
        self._audio.stop_reading()
        if self._cleaned:
            return
        self._cleaned = True
        self._audio.on_task_finished()

    def _voice_client_after(self, e: Exception | None) -> None:
        """Apply the player result unless the item already moved on."""
        if self._status not in {
            PlaybackStatus.WAITING,
            PlaybackStatus.PLAYING,
            PlaybackStatus.PAUSED,
        }:
            return
        if e is None:
            self.finish()
        else:
            self.fail(VoiceClientError(e))
