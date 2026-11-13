"""Layout description of an audio payload: container, rate, channels, width."""

import logging
from dataclasses import dataclass
from enum import Enum


class AudioFormatType(Enum):
    """Encoding or container of an audio payload.

    Attributes:
        PCM: Raw interleaved samples without a header.
        WAV: RIFF/WAVE container wrapping PCM samples.
        MP3: MPEG-1/2 Audio Layer III frames.
        OPUS: Opus packets, normally carried inside an Ogg stream.
    """

    PCM = "pcm"
    WAV = "wav"
    MP3 = "mp3"
    OPUS = "opus"


@dataclass(frozen=True)
class AudioFormat:
    """Byte layout of an audio payload.

    Instances are frozen and hashable, so a single instance can be shared
    between the producing and consuming side of a stream and used as a mapping
    key.

    Attributes:
        type: Encoding or container of the payload.
        sample_rate: Sample rate in Hz, for example 48000.
        channels: Number of interleaved channels, for example 2.
        sample_width: Width of one sample in bytes, for example 2 for signed
            16-bit samples.
    """

    type: AudioFormatType
    sample_rate: int | None = None
    channels: int | None = None
    sample_width: int | None = None

    def __post_init__(self) -> None:
        """Hook that validates the layout.

        Every field combination is accepted at the moment. The hook exists so
        validation and derived values (frame size, bytes per second) can be
        added without changing the constructor signature.
        """
        # TODO: verify fields
        logging.getLogger(__file__).warn(
            f"{self.__class__.__name__}: post_init is not implemented"
        )
