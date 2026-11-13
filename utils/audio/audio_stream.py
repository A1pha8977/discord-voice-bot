"""In-memory audio pipe with backpressure.

``AudioStream`` joins a producing task and a consuming task through a bounded
byte buffer: the producer suspends while the buffer is full, the consumer
suspends while it is empty, and the two make progress without polling.

The stream is built for one producer task and one consumer task. Concurrent
calls on the same side serialize on a per-side lock, so several producers
append whole chunks in turn.
"""

import asyncio
from io import UnsupportedOperation

from utils.audio.audio_reader import AudioReader
from utils.audio.audio_writer import AudioWriter
from utils.audio.errors import AudioStreamClosedError, AudioStreamFailedError

from .audio_format import AudioFormat


class AudioStream(AudioReader, AudioWriter):
    """Bounded byte pipe between one producer task and one consumer task.

    The stream owns a buffer of ``max_buffer_size`` bytes. ``write()`` appends
    to it and suspends the producer when the buffer is full; ``read()`` removes
    bytes and suspends the consumer when the buffer is empty.

    Termination travels in both directions. The producer side ends with
    ``write_eof()`` for complete audio or ``write_fail()`` for failed
    production, and both wake the consumer. The consumer side ends with
    ``read_close()``, which wakes a producer blocked on a full buffer.

    Example:
        Producer task::

            await stream.write(chunk)
            await stream.write_eof()

        Consumer task::

            try:
                while chunk := await stream.read():
                    await play(chunk)
            finally:
                await stream.read_close()
    """

    def __init__(self, format: AudioFormat, max_buffer_size: int | None = None) -> None:
        """Create an empty stream for the given layout.

        Args:
            format: Layout of the bytes that travel through the stream.
            max_buffer_size: Buffer capacity in bytes. Defaults to 64 KiB,
                about 0.34 s of 48 kHz 16-bit stereo audio.

        Raises:
            ValueError: ``max_buffer_size`` is zero or negative.
        """
        max_buffer_size = max_buffer_size if max_buffer_size is not None else 2**16
        if max_buffer_size <= 0:
            raise ValueError(
                f"Audio Stream: buffer_size must > 0 got {max_buffer_size}"
            )
        self._audio_format = format
        self._max_buffer_size = max_buffer_size
        self._buffer = bytearray()
        self._reader_lock = asyncio.Lock()
        self._writer_lock = asyncio.Lock()
        self._cond = asyncio.Condition()
        self._eof = False
        self._failed_reason: BaseException | None = None
        self._closed = False

    def format(self) -> AudioFormat:
        """Return the layout of the bytes travelling through this stream."""
        return self._audio_format

    def unread_size(self) -> int:
        """Return the number of buffered bytes awaiting ``read()``.

        Returns:
            Current buffer length in bytes.

        Note:
            Blocks while another ``write()`` is in progress.
        """
        return len(self._buffer)

    async def read(self, max_size: int | None = None) -> bytes:
        """Take up to ``max_size`` bytes out of the buffer.

        Args:
            max_size: Upper bound of the returned chunk in bytes. ``None``
                reads up to the buffer capacity. ``0`` returns an empty chunk
                straight away.

        Returns:
            The bytes just removed from the buffer, or ``b""`` once the
            producer signalled its end and the buffer is empty.

        Raises:
            ValueError: ``max_size`` is negative.
            io.UnsupportedOperation: The consumer side is already closed.
            AudioStreamFailedError: The producer reported a failure. The
                original error is available through ``__cause__``.

        Note:
            Blocks while the buffer is empty and the producer keeps running.
            Bytes leave the buffer in the order they were written. Concurrent
            calls serialize on an internal reader lock. Once the producer has
            reported a failure, the call raises before draining the buffer.
        """
        max_size = self._max_buffer_size if max_size is None else max_size
        if max_size < 0:
            raise ValueError(f"read: max_size must >= 0 got {max_size}")
        if max_size == 0:
            return b""
        async with self._reader_lock, self._cond:
            if self._closed:
                raise UnsupportedOperation("AudioStream: read() after closed")
            await self._cond.wait_for(lambda: bool(self._buffer) or self._eof)
            if self._failed_reason:
                raise AudioStreamFailedError from self._failed_reason
            actual_read_size = min(len(self._buffer), max_size)
            data = bytes(self._buffer[:actual_read_size])
            del self._buffer[:actual_read_size]
            self._cond.notify_all()
            return data

    async def write(self, data: bytes) -> None:
        """Append ``data`` to the buffer, waiting for room when it is full.

        Args:
            data: Payload to append. Bytes are taken from this object chunk by
                chunk as room frees up, so a mutable buffer shared with the
                caller can change between chunks.

        Raises:
            io.UnsupportedOperation: The producer already signalled its end.
            AudioStreamClosedError: The consumer side is closed.

        Note:
            Suspends the producer while the buffer is full, which paces the
            producer with the consumer. The call returns after every byte of
            ``data`` has been accepted. An empty payload appends nothing and
            returns immediately.
        """
        async with self._writer_lock, self._cond:
            if self._eof:
                raise UnsupportedOperation("AudioStream: write() after eof")
            view = memoryview(data)
            written_size = 0
            while written_size < len(view):
                await self._cond.wait_for(
                    lambda: self._remain_buffer_size or self._closed
                )
                if self._closed:
                    raise AudioStreamClosedError
                can_write_size = min(self._remain_buffer_size, len(view) - written_size)
                self._buffer.extend(view[written_size : can_write_size + written_size])
                written_size += can_write_size
                self._cond.notify_all()

    async def write_eof(self) -> None:
        """Mark the producer side as complete.

        Wakes waiting readers, which drain the buffered bytes and then receive
        ``b""``. Later ``write()`` calls raise ``io.UnsupportedOperation``.
        """
        async with self._writer_lock, self._cond:
            self._eof = True
            self._cond.notify_all()

    async def at_eof(self) -> bool:
        """Report whether the produced audio has been fully consumed.

        Returns:
            True once the producer signalled its end and the buffer is drained.

        Note:
            Blocks while another ``read()`` is in progress. The producer side
            owns the end-of-stream state, so ``read_close()`` on the consumer
            side leaves the reported value unchanged.
        """
        async with self._reader_lock, self._cond:
            return self._eof and not self._buffer

    async def write_fail(self, e: BaseException) -> None:
        """Terminate the stream because the producer failed.

        Args:
            e: Error that stopped the producer, chained into the
                ``AudioStreamFailedError`` delivered to readers.

        Note:
            Wakes waiting readers, which raise ``AudioStreamFailedError`` with
            ``e`` as ``__cause__``. Buffered bytes stay in the buffer while
            readers raise, so ``unread_size()`` keeps reporting them. Later
            ``write()`` calls raise ``io.UnsupportedOperation``.
        """
        async with self._writer_lock, self._cond:
            self._failed_reason = e
            self._eof = True
            self._cond.notify_all()

    async def read_close(self) -> None:
        """Shut the stream down from the consumer side.

        Wakes a producer blocked on a full buffer, which then raises
        ``AudioStreamClosedError``. Later ``read()`` calls raise
        ``io.UnsupportedOperation``. Buffered bytes stay in the buffer and
        ``unread_size()`` keeps reporting them.

        Note:
            Idempotent. Calling it from the consumer shutdown path, for example
            in a ``finally`` block, releases a blocked producer.
        """
        async with self._reader_lock, self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def _remain_buffer_size(self) -> int:
        """Number of bytes ``write()`` can append without waiting."""
        return self._max_buffer_size - len(self._buffer)
