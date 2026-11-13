"""Client contract for text-to-speech providers."""

from abc import ABC, abstractmethod

from services.tts.tts_config import TTSConfig
from utils.audio.audio_reader import AudioReader


class TTSClient(ABC):
    """Turn text into an audio stream for one provider.

    A client validates that the configuration targets its provider, starts
    producing audio in the background and returns the audio stream immediately.
    The returned reader delivers the produced bytes and reports production
    failures to the consumer.
    """

    @abstractmethod
    def tts_provider_id(self) -> str:
        """Return the identifier of the provider this client implements.

        Returns:
            Provider identifier such as ``DEEPSEEK`` or ``FISH_AUDIO``.
        """
        raise NotImplementedError

    @abstractmethod
    def generate(self, text: str, config: TTSConfig) -> AudioReader:
        """Start producing speech for ``text`` and return the audio stream.

        Args:
            text: Text to synthesize.
            config: Provider configuration. Its provider identifier must match
                this client.

        Returns:
            Reader over the produced audio. It yields bytes in the layout
            reported by ``config.audio_format()`` and ends with an empty chunk
            once production completes.

        Raises:
            ValueError: ``config`` targets a different provider.
            RuntimeError: No event loop is running in the calling thread.

        Note:
            Production continues on the running event loop after this call
            returns. The consumer owns the returned reader and closes it when
            playback stops so the producer is released.
        """
        raise NotImplementedError
