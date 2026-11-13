"""Producer-side contract for audio streams."""

from abc import abstractmethod

from utils.audio.audio import Audio


class AudioWriter(Audio):
    """Producer-side contract for an audio stream.

    A writer hands ownership of the bytes to the stream: once ``write()``
    returns, the stream holds them and the caller can reuse its own buffer.

    The intended driving pattern is one producer task that calls ``write()``
    for every chunk and finishes with ``write_eof()`` on success or
    ``write_fail()`` on error.
    """

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Hand ``data`` over to the stream, waiting for room when it is full.

        Args:
            data: Payload to append. The call returns after every byte of the
                payload has been accepted.

        Raises:
            AudioStreamClosedError: The consumer side is closed.
            io.UnsupportedOperation: The producer already signalled its end.
        """
        raise NotImplementedError

    @abstractmethod
    async def write_eof(self) -> None:
        """Mark the produced audio as complete.

        Wakes waiting readers, which deliver the remaining buffered bytes and
        then observe the end of the stream. Later ``write()`` calls raise
        ``io.UnsupportedOperation``.
        """
        raise NotImplementedError

    @abstractmethod
    async def write_fail(self, e: BaseException) -> None:
        """Terminate the stream because the producer failed.

        Args:
            e: Error that stopped the producer. It is chained into the
                ``AudioStreamFailedError`` delivered to readers.

        Note:
            Wakes waiting readers, which raise ``AudioStreamFailedError`` with
            ``e`` as ``__cause__``. Later ``write()`` calls raise
            ``io.UnsupportedOperation``.
        """
        raise NotImplementedError
