"""Playback source of one item: volume control plus revocable reads.

The source wraps the upstream in ``discord.PCMVolumeTransformer`` so the volume
can change while the audio plays, and it gates the reads so a preempted item can
keep its upstream alive for the next playback.
"""

import asyncio
import audioop
import threading
from collections.abc import Callable

from discord import AudioSource, PCMVolumeTransformer

ReadyCallback = Callable[[BaseException | None], None]


class PlaybackAudioSource(AudioSource):
    """Own an upstream PCM source for the lifetime of one playback item.

    The upstream is wrapped in ``discord.PCMVolumeTransformer``, so ``read``
    returns PCM with the current volume applied and ``set_volume`` takes effect
    on the next read. Only PCM sources are accepted; an opus source raises
    ``discord.ClientException`` from the transformer.

    The upstream is kept alive while another item preempts this one:
    ``stop_reading`` blocks the reads of a player that is about to be stopped and
    ``resume_reading`` reopens them for the next playback. A read that already
    started inside the upstream cannot be interrupted; it returns its bytes or
    fails once the upstream is released, and Discord's player discards that
    error.

    Discord's player calls ``cleanup`` on the source it was given, so the cleanup
    is ignored and ``on_task_finished`` releases the upstream instead.
    """

    def __init__(
        self, audio_source: AudioSource, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Wrap ``audio_source`` with the initial volume.

        Args:
            audio_source: PCM source owned by the playback item.
            loop: Event loop that runs the prefetch and the ready callbacks.
        """
        self._volume_source = PCMVolumeTransformer(audio_source)
        self._lock = threading.Lock()
        self._loop = loop
        self._future: asyncio.Future[bytes] | None = None
        self._ready = False
        self._ready_error: BaseException | None = None
        self._ready_callback: ReadyCallback | None = None
        self._reading = True

    @property
    def audio_source(self) -> AudioSource:
        """Return the upstream source below the volume transformer."""
        return self._volume_source.original

    def read(self) -> bytes:
        """Return the next chunk with the volume applied.

        The chunk read by the prefetch is returned by the first call once the
        source is ready, so the upstream produces audio before the player
        starts.

        Returns:
            PCM bytes, or empty bytes while reading is stopped.

        Raises:
            RuntimeError: The source is not ready, so it must not be submitted
                to the player yet; see ``is_ready``.
        """
        if not self.is_ready():
            raise RuntimeError("the source is not ready for playback")
        with self._lock:
            if not self._reading:
                return b""
        if self._future is not None:
            chunk = self._future.result()
            self._future = None
            return self._apply_volume(chunk)
        return self._volume_source.read()

    def _apply_volume(self, chunk: bytes) -> bytes:
        """Scale ``chunk`` with the current volume like the transformer does."""
        return audioop.mul(chunk, 2, min(self._volume_source.volume, 2.0))

    def is_opus(self) -> bool:
        """Report whether the upstream carries opus packets."""
        return self._volume_source.is_opus()

    def cleanup(self) -> None:
        """Ignore the player cleanup; ``on_task_finished`` releases the upstream."""
        return

    def stop_reading(self) -> None:
        """Block reads without consuming the upstream audio."""
        self._discard_fetch()
        with self._lock:
            self._reading = False

    def resume_reading(self) -> None:
        """Allow reads again for the next playback."""
        with self._lock:
            self._reading = True

    def get_volume(self) -> float:
        """Return the volume multiplier applied to every read."""
        return self._volume_source.volume

    def set_volume(self, volume: float) -> None:
        """Set the volume multiplier applied to every later read.

        Args:
            volume: ``0.0`` is silence and ``1.0`` keeps the upstream level.

        Raises:
            ValueError: The volume is outside ``0.0`` to ``2.0``.
        """
        if not 0.0 <= volume <= 2.0:
            raise ValueError(f"volume must be between 0.0 and 2.0, got {volume}")
        self._volume_source.volume = volume

    def on_task_finished(self) -> None:
        """Release the upstream source once the item reached a terminal state."""
        self._discard_fetch()
        self._volume_source.cleanup()

    def _discard_fetch(self) -> None:
        """Drop a pending prefetch, or mark a finished one as retrieved.

        A pending prefetch is cancelled so the executor thread stops as soon as
        the upstream returns; a finished prefetch is asked for its result so
        asyncio does not log an unretrieved exception. A discarded prefetch
        never notifies a ready callback.
        """
        future = self._future
        self._future = None
        self._ready_callback = None
        self._ready_error = None
        if future is None:
            return
        if not future.done():
            future.cancel()
            return
        if not future.cancelled():
            future.exception()

    def _pre_fetch(self) -> bytes:
        """Read one chunk of the upstream source on the executor thread."""
        return self.audio_source.read()

    def preload(self) -> None:
        """Start reading the first upstream chunk ahead of the playback.

        This is the only thing that starts the prefetch, so ``is_ready``,
        ``ready_error`` and ``set_ready_callback`` do nothing useful until it is
        called. The read runs on the executor thread, so an upstream that
        produces no audio yet never blocks the player thread. Repeated calls do
        nothing, and a source whose prefetch was discarded is not preloaded
        again.
        """
        if self._future is not None or self._ready:
            return
        self._future = self._loop.run_in_executor(None, self._pre_fetch)
        self._future.add_done_callback(self._on_fetch_done)

    def is_ready(self) -> bool:
        """Report whether the prefetched first chunk is available.

        The result stays False until ``preload`` started the prefetch, so a
        source that was never preloaded is never ready.
        """
        return self._ready

    def ready_error(self) -> BaseException | None:
        """Return the error of a failed prefetch, or None.

        Only a prefetch started by ``preload`` can store an error.
        """
        return self._ready_error

    def set_ready_callback(self, callback: ReadyCallback | None) -> None:
        """Register the one-shot callback fired when the prefetch completes.

        The callback runs on the event loop with ``None`` when the chunk is
        available or with the prefetch error when the upstream failed. The
        prefetch must be started with ``preload``, otherwise the source never
        becomes ready and the callback never fires. It fires at once when the
        prefetch already finished, and never fires when the prefetch was
        discarded.

        Args:
            callback: Callback to notify, or None to drop the registration.
        """
        self._ready_callback = callback
        if callback is None or self._future is None:
            return
        if self._ready or self._ready_error is not None:
            self._loop.call_soon(self._notify_ready)

    def _on_fetch_done(self, future: asyncio.Future[bytes]) -> None:
        """Store the prefetch outcome and notify the registered callback."""
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            self._ready_error = error
        else:
            self._ready = True
        self._notify_ready()

    def _notify_ready(self) -> None:
        """Call the registered ready callback once and forget it."""
        callback = self._ready_callback
        if callback is None:
            return
        self._ready_callback = None
        callback(self._ready_error)
