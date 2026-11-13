"""Consumer-side contract for audio streams."""

from abc import abstractmethod

from utils.audio.audio import Audio


class AudioReader(Audio):
    """Consumer-side contract for an audio stream.

    A reader takes ownership of the bytes it consumes: a chunk returned by
    ``read()`` has left the stream, and later calls return newer bytes.

    The intended driving pattern is one consumer task that calls ``read()``
    until the produced audio is exhausted, then finishes with ``read_close()``
    from its shutdown path.
    """

    @abstractmethod
    def unread_size(self) -> int:
        """Return how many bytes are ready to be read right now.

        Returns:
            Number of buffered bytes that an immediate ``read()`` can deliver
            without waiting for the producer.
        """
        raise NotImplementedError

    @abstractmethod
    async def read(self, max_size: int | None = None) -> bytes:
        """Return up to ``max_size`` buffered bytes, waiting when none are ready.

        Args:
            max_size: Upper bound of the returned chunk in bytes. ``None`` lets
                the implementation pick its natural chunk size.

        Returns:
            A chunk of at most ``max_size`` bytes, or an empty ``bytes`` once
            the producer has finished and every buffered byte has been read.

        Raises:
            ValueError: ``max_size`` is negative.
            io.UnsupportedOperation: The consumer side is already closed.
            AudioStreamFailedError: The producer reported a failure. The
                original error is available through ``__cause__``.

        Note:
            The call blocks while the stream holds no bytes and the producer is
            still running.
        """
        raise NotImplementedError

    @abstractmethod
    async def at_eof(self) -> bool:
        """Report whether every produced byte has been read.

        Returns:
            True once the producer signalled its end and the buffered bytes are
            drained.
        """
        raise NotImplementedError

    @abstractmethod
    async def read_close(self) -> None:
        """Stop consuming and shut the stream down from the consumer side.

        Releases the producer, including one blocked on a full buffer, and
        ends the audio flow for both sides. The call is idempotent.

        Note:
            Calling it from the consumer shutdown path, for example in a
            ``finally`` block, keeps the producer stopped when the consumer is
            cancelled mid-playback.
        """
        raise NotImplementedError
