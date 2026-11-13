"""Configuration contract shared by every TTS provider."""

from abc import ABC, abstractmethod

from utils.audio.audio_format import AudioFormat


class TTSConfig(ABC):
    """Describe one text-to-speech request for a single provider.

    A configuration carries the provider identity, the profile name, the audio
    layout the provider returns and the provider-specific connection and payload
    fields. Clients read these values and never mutate the configuration.

    Every accessor that returns a ``dict`` builds a new dictionary on each call.
    The caller owns that dictionary and may modify it, for example to add an
    authorization header, without affecting the configuration or the values
    returned by later calls.
    """

    @abstractmethod
    def tts_provider_id(self) -> str:
        """Return the identifier of the provider this configuration targets.

        Returns:
            Provider identifier such as ``DEEPSEEK`` or ``FISH_AUDIO``.
        """
        raise NotImplementedError

    @abstractmethod
    def voice_name(self) -> str:
        """Return the voice name that is shown to users.

        Returns:
            Voice name displayed to users.
        """
        raise NotImplementedError

    @abstractmethod
    def audio_format(self) -> AudioFormat:
        """Return the byte layout of the audio the provider produces.

        Returns:
            Layout describing container, sample rate, channels and sample
            width of the produced payload.
        """
        raise NotImplementedError

    @abstractmethod
    def http_headers(self) -> dict:
        """Return the HTTP headers required to open a provider connection.

        Returns:
            A new dictionary built on every call. The caller may modify it, for
            example to add credentials, without affecting the configuration or
            the headers returned by later calls.
        """
        raise NotImplementedError

    @abstractmethod
    def http_params(self) -> dict:
        """Return the HTTP or connection query parameters of this configuration.

        Returns:
            A new dictionary built on every call. The caller may modify it
            without affecting the configuration or later calls.
        """
        raise NotImplementedError

    @abstractmethod
    def payload_params(self) -> dict:
        """Return the provider-specific request payload fields.

        Returns:
            A new dictionary built on every call, containing only the fields
            this provider accepts. The caller may modify it, for example to add
            the text of the request, without affecting the configuration or
            later calls.
        """
        raise NotImplementedError
