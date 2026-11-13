"""Base class for objects that expose an audio layout."""

from abc import ABC, abstractmethod

from .audio_format import AudioFormat


class Audio(ABC):
    """An object that carries an audio layout.

    ``format()`` is the single point where the byte interpretation of the
    carried audio is published, so a producer and a consumer can agree on it
    before exchanging data. Sinks that need extra negotiation (for example a
    voice client) adapt the reported layout themselves.
    """

    @abstractmethod
    def format(self) -> AudioFormat:
        """Return the layout of the audio carried by this object.

        The method is intentionally synchronous: the layout is fixed when the
        object is created, so reading it is a pure in-memory access.

        Returns:
            The layout describing sample rate, channel count and sample width.
        """
        raise NotImplementedError
