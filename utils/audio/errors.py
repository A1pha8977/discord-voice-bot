"""Exception hierarchy for the audio utilities.

These errors are guard checks: they report that a stream was used outside its
valid state, such as a read after close, a write after eof, or an invalid
format. The class name, the message and the traceback carry the whole context.

``AudioStreamFailedError`` reports a real runtime failure and carries the
original error through ``__cause__`` (``raise ... from original``), which keeps
the original traceback intact and lets callers match the specific error.

Argument validation (negative buffer size, negative read size) reports through
``ValueError``, which keeps caller mistakes separate from stream state.
"""

__all__ = [
    "AudioError",
    "AudioFormatError",
    "AudioStreamClosedError",
    "AudioStreamError",
    "AudioStreamFailedError",
]


class AudioError(Exception):
    """Base class for every error raised by the audio utilities."""


class AudioFormatError(AudioError, ValueError):
    """Raised when an ``AudioFormat`` instance describes an invalid layout.

    Inherits ``ValueError``, so callers that validate values with
    ``except ValueError`` keep working, mirroring ``json.JSONDecodeError``.
    """


class AudioStreamError(AudioError):
    """Base class for runtime errors of a single audio stream.

    Catching it covers every stream-level failure, including a failed producer.
    """


class AudioStreamClosedError(AudioStreamError):
    """Raised when a write targets a stream the consumer has closed.

    A consumer closing the stream is the normal teardown path when playback
    stops before the producer finishes; the error releases the producer.
    """


class AudioStreamFailedError(AudioStreamError):
    """Raised when the stream was terminated by a failure of its producer.

    The original error travels through ``__cause__`` for readers that need the
    root cause.
    """
