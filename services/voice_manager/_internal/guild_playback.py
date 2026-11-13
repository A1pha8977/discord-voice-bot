"""Per-guild playback state: priority queue, worker task, current item and volume."""

import asyncio
import logging
from asyncio.queues import QueueFull
from itertools import count

from services.voice_manager.errors import VoiceManagerError, VoiceManagerQueueFullError
from services.voice_manager.playback_item import PlaybackItem, PlaybackStatus

logger = logging.getLogger(__name__)


class GuildPlayback:
    """Own the playback state of one guild and run its items serially.

    The queue, the worker task, the current item and the volume of every kind
    belong to this object. The queue is ordered by ``PlaybackPriority``: a new
    item that outranks the current one interrupts it and is played next, and the
    interrupted item keeps its source so it resumes from the same position.
    Each item plays with the volume stored for its ``PlaybackSpec.kind``.
    All methods run on the event loop thread that owns the guild.
    """

    READINESS_TIMEOUT = 15.0
    """Seconds to wait for the source of an item before requeueing it."""

    def __init__(self, guild_id: int, *, default_volume: float = 1.0) -> None:
        """Create the state of one guild.

        Args:
            guild_id: Guild whose items are played by this object.
            default_volume: Volume multiplier used for a kind that has no volume
                yet, and for every item when the submitter never changed it.

        Raises:
            ValueError: The volume is outside ``0.0`` to ``2.0``.
        """
        if not 0.0 <= default_volume <= 2.0:
            raise ValueError(
                f"volume must be between 0.0 and 2.0, got {default_volume}"
            )
        self.guild_id = guild_id
        self._default_volume = default_volume
        self._volume = dict[str, float]()
        self._queue = asyncio.PriorityQueue[tuple[int, int, PlaybackItem]](20)
        self._task_seq = count()
        self._running = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None
        self._current: PlaybackItem | None = None
        self._current_seq: int | None = None

    def is_full(self) -> bool:
        """Report whether the pending queue has reached its capacity."""
        return self._queue.full()

    def plan(
        self,
        task: PlaybackItem,
    ) -> None:
        """Append ``task`` to the queue or fail it when it cannot be accepted.

        The item is queued by its priority. Its ready callback is registered so
        the item can interrupt the current playback once it has audio; the
        prefetch starts right away only when the item is about to play, so a long
        queue does not keep several upstreams busy.

        Args:
            task: Playback item created for this guild.

        Raises:
            ValueError: The item belongs to another guild.

        Note:
            A closed playback or a full queue fails the item instead of raising,
            so the caller inspects the item status afterwards.
        """
        if task._guild_id != self.guild_id:
            raise ValueError(
                f"guild_id mismatch got {task._guild_id} expected {self.guild_id}"
            )
        if self._closed:
            task.fail(RuntimeError("guild playback is closed"))
            return
        try:
            self._queue.put_nowait((task.spec.priority, next(self._task_seq), task))
        except QueueFull:
            task.fail(VoiceManagerQueueFullError())
            return
        task.set_ready_callback(lambda error: self._on_task_ready(task, error))
        if self._is_upcoming(task):
            task.preload()

    def _is_upcoming(self, task: PlaybackItem) -> bool:
        """Report whether ``task`` is about to play or may preempt the current item."""
        current = self._current
        return current is None or task.spec.priority < current.spec.priority

    def _on_task_ready(self, task: PlaybackItem, error: BaseException | None) -> None:
        """Fail the item on a prefetch error, otherwise try to preempt the current one."""
        if self._closed or task.status() is not PlaybackStatus.WAITING:
            return
        if error is not None:
            try:
                task.fail(error)
            except RuntimeError:
                logger.exception("Failed to fail the item: guild=%s", self.guild_id)
            return
        self._try_interrupt_current(task)

    def _try_interrupt_current(self, new_task: PlaybackItem) -> None:
        """Interrupt the current item when ``new_task`` outranks it and requeue it.

        The new item must already be ready, which the ready callback guarantees.
        The interrupted item keeps its source, so it continues from the
        interrupted position when the worker picks it up again. A current item
        that is not playing or paused, or an item that cannot be requeued because
        the queue is full, is left alone; the full queue case fails the
        interrupted item instead.
        """
        current = self._current
        if current is None or new_task.spec.priority >= current.spec.priority:
            return
        if current.status() not in {PlaybackStatus.PLAYING, PlaybackStatus.PAUSED}:
            return
        assert self._current_seq is not None
        current.interrupt()
        # Requeue with the sequence assigned on the first enqueue: a fresh
        # sequence would place the item behind the items that were already
        # waiting with the same priority, while the original one makes the
        # resumed item land in front of them without touching their order.
        try:
            self._queue.put_nowait((current.spec.priority, self._current_seq, current))
        except QueueFull:
            current.fail(VoiceManagerQueueFullError())

    def close(self) -> None:
        """Request worker shutdown and cancel the current and queued items.

        Repeated calls preserve the closed state. Use ``wait_closed()`` to wait
        for the asyncio worker to exit. Cancelling an item releases its source,
        so the caller must not clean the audio again.
        """
        if self._closed:
            return
        self._closed = True
        self._running = False
        if self._task is not None:
            self._task.cancel()
        if self._current is not None:
            self._cancel_task(self._current)
            self._current = None
            self._current_seq = None
        while not self._queue.empty():
            task = self._queue.get_nowait()[2]
            try:
                self._cancel_task(task)
            finally:
                self._queue.task_done()

    def is_playing(self) -> bool:
        """Report whether an item is currently playing rather than paused."""
        return (
            self._current is not None
            and self._current.status() == PlaybackStatus.PLAYING
        )

    def is_paused(self) -> bool:
        """Report whether the current item is paused."""
        return (
            self._current is not None
            and self._current.status() == PlaybackStatus.PAUSED
        )

    def current_task(self) -> PlaybackItem | None:
        """Return the item being played, or None when the worker is idle."""
        return self._current

    async def wait_closed(self) -> None:
        """Wait for the asyncio worker to exit after a close request."""
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    def start(self) -> None:
        """Start one worker; repeated calls reuse it until the manager closes.

        Raises:
            RuntimeError: The playback is already closed.
        """
        if self._closed:
            raise RuntimeError("guild playback is closed")
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._work())
        self._running = True

    def pause(self) -> bool:
        """Pause the item that is currently playing.

        Returns:
            True when the item moved to ``PAUSED``; False when nothing is
            playing or the item is already paused.

        Raises:
            RuntimeError: The playback is closed or its worker is not running.
        """
        self._require_running()
        if self._current is None or self._current.status() != PlaybackStatus.PLAYING:
            return False
        self._current.pause()
        return True

    def resume(self) -> bool:
        """Resume the paused item.

        Returns:
            True when the item moved back to ``PLAYING``; False when nothing is
            paused.

        Raises:
            RuntimeError: The playback is closed or its worker is not running.
        """
        self._require_running()
        if self._current is None or self._current.status() != PlaybackStatus.PAUSED:
            return False
        self._current.resume()
        return True

    def cancel(self) -> bool:
        """Cancel the item that is playing or paused.

        Returns:
            True when an item was cancelled; False when the worker is idle.

        Raises:
            RuntimeError: The playback is closed or its worker is not running.
        """
        self._require_running()
        if self._current is None:
            return False
        self._cancel_task(self._current)
        return True

    def get_volume(self, kind: str) -> float:
        """Return the volume multiplier stored for ``kind``.

        Args:
            kind: Group name of the playback, for example ``tts``.

        Returns:
            The stored volume, or the default volume of this object when no
            volume was set for the kind yet.

        Raises:
            ValueError: The kind is empty or contains only whitespace.
        """
        if not kind.strip():
            raise ValueError(f"kind must not be empty, got {kind!r}")
        if kind not in self._volume:
            return self._default_volume
        return self._volume[kind]

    def set_volume(self, kind: str, volume: float) -> None:
        """Set the volume of ``kind`` for the current item and the later ones.

        The item that is currently playing receives the value at once when its
        kind matches; other items receive it when the worker takes them.

        Args:
            kind: Group name of the playback, for example ``tts``.
            volume: ``0.0`` is silence and ``1.0`` keeps the source level.

        Raises:
            ValueError: The kind is empty or contains whitespace only, or the
                volume is outside ``0.0`` to ``2.0``.
        """
        if not kind.strip():
            raise ValueError(f"kind must not be empty, got {kind!r}")
        if not 0.0 <= volume <= 2.0:
            raise ValueError(f"volume must be between 0.0 and 2.0, got {volume}")
        self._volume[kind] = volume
        if self._current is not None and self._current.spec.kind == kind:
            self._current.set_volume(volume)

    def _require_running(self) -> None:
        """Raise when the playback cannot accept pause or resume requests."""
        if self._closed:
            raise RuntimeError("guild playback is closed")
        if self._task is None or self._task.done():
            raise RuntimeError("guild playback worker is not running")

    def _cancel_task(self, task: PlaybackItem) -> None:
        """Cancel ``task``, logging instead of propagating cleanup failures."""
        try:
            task.cancel()
        except Exception:
            logger.exception("Failed to cancel playback item: guild=%s", self.guild_id)

    async def _work(self) -> None:
        """Play queued items one after another until the playback closes.

        Each iteration takes one item, applies the current volume, waits until the
        source has its first chunk and then plays. An interrupted item is left to
        the ready callback for the requeue, so this loop just moves on to the
        item that outranks it. Every ``get`` is balanced by one ``task_done``.
        """
        try:
            while self._running:
                _, seq, task = await self._queue.get()
                self._current = task
                self._current_seq = seq
                task.set_volume(self.get_volume(task.spec.kind))
                task.preload()
                try:
                    if not await self._wait_until_ready(task):
                        self._requeue_unready(task)
                    elif task.status() in {
                        PlaybackStatus.WAITING,
                        PlaybackStatus.INTERRUPTED,
                    }:
                        task.play()
                        while task.status() not in {
                            PlaybackStatus.CANCELLED,
                            PlaybackStatus.FAILED,
                            PlaybackStatus.FINISHED,
                            PlaybackStatus.INTERRUPTED,
                        }:
                            await asyncio.sleep(0.05)
                except Exception as error:
                    if isinstance(error, VoiceManagerError):
                        logger.warning(
                            "Playback item failed: guild=%s playback=%s reason=%r",
                            self.guild_id,
                            task.spec.name,
                            error,
                        )
                    else:
                        logger.exception(
                            "Playback item failed: guild=%s playback=%s",
                            self.guild_id,
                            task.spec.name,
                        )
                    try:
                        task.fail(error)
                    except Exception:
                        logger.exception(
                            "Failed to finalize playback item: guild=%s", self.guild_id
                        )
                finally:
                    self._current = None
                    self._current_seq = None
                    self._queue.task_done()
        finally:
            self._running = False

    async def _wait_until_ready(self, task: PlaybackItem) -> bool:
        """Wait up to ``READINESS_TIMEOUT`` for the source of ``task``.

        Returns:
            True when the source became ready or the item left the waiting
            states on its own; False when the timeout expired first.
        """
        waited = 0.0
        while not task.is_ready() and task.status() in {
            PlaybackStatus.WAITING,
            PlaybackStatus.INTERRUPTED,
        }:
            if waited >= self.READINESS_TIMEOUT:
                return False
            await asyncio.sleep(0.05)
            waited += 0.05
        return True

    def _requeue_unready(self, task: PlaybackItem) -> None:
        """Put a task that never became ready back at the tail of the queue.

        The item keeps its priority but receives a fresh sequence, so the items
        that were waiting behind it are played first instead of blocking on a
        source that produces no audio.
        """
        logger.warning(
            "Playback item is not ready, requeued: guild=%s playback=%s",
            self.guild_id,
            task.spec.name,
        )
        try:
            self._queue.put_nowait((task.spec.priority, next(self._task_seq), task))
        except QueueFull:
            task.fail(VoiceManagerQueueFullError())
