import asyncio
import logging
from typing import cast

import aiohttp
import msgpack

from services.tts.tts_client import TTSClient
from services.tts.tts_config import TTSConfig
from utils.audio.audio_format import AudioFormat, AudioFormatType
from utils.audio.audio_reader import AudioReader
from utils.audio.audio_stream import AudioStream
from utils.audio.audio_writer import AudioWriter

_logger = logging.getLogger(__name__)
_TTS_PROVIDER_ID = "FISH_AUDIO"


class FishAudioTTSClient(TTSClient):
    """Stream speech from the Fish Audio live TTS WebSocket endpoint.

    One request opens a WebSocket connection, sends a start event with the
    configuration, forwards the text stream, then reads MessagePack audio
    frames until the server reports completion. Production failures are
    delivered to the reader instead of being raised from ``generate()``.
    """

    BASE_API_URL = "wss://api.fish.audio/v1/tts/live"

    def __init__(self, api_key: str) -> None:
        """Create a client bound to one Fish Audio API key.

        Args:
            api_key: Fish Audio API key used as a bearer token.
        """
        self._api_key = api_key
        self._tasks: set[asyncio.Task[None]] = set()

    def tts_provider_id(self) -> str:
        """Return the provider identifier this client implements."""
        return _TTS_PROVIDER_ID

    def generate(self, text: str, config: TTSConfig) -> AudioReader:
        """Start producing speech for ``text`` and return the audio stream.

        Args:
            text: Text to synthesize.
            config: Fish Audio configuration. Its provider identifier must be
                ``FISH_AUDIO``.

        Returns:
            Reader over the produced audio, in the layout reported by
            ``config.audio_format()``.

        Raises:
            ValueError: ``config`` targets a different provider.

        Note:
            Production runs in a background task that the client keeps alive
            until completion. The consumer owns the returned reader and closes
            it when playback stops.
        """
        if config.tts_provider_id() != _TTS_PROVIDER_ID:
            raise ValueError(
                f"{type(self).__name__} unexpected config: {config.tts_provider_id()}"
            )
        loop = asyncio.get_running_loop()
        audio_stream = AudioStream(config.audio_format())
        task = loop.create_task(
            self._call_api_then_write_back(text, config, audio_stream)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return audio_stream

    async def _call_api_then_write_back(
        self, text: str, config: TTSConfig, audio_writer: AudioWriter
    ) -> None:
        """Stream one request into ``audio_writer``.

        Args:
            text: Text to synthesize.
            config: Configuration supplying the connection headers and the
                start event fields.
            audio_writer: Producer side of the audio stream that receives the
                audio chunks.

        Note:
            Ends the stream with ``write_eof()`` on success and ``write_fail()``
            with the original error on failure or cancellation. Unknown events
            are ignored so future protocol additions stay compatible.
        """
        headers = dict(config.http_headers())
        headers["Authorization"] = f"Bearer {self._api_key}"
        start_event = {
            "event": "start",
            "request": {"text": "", **config.payload_params()},
        }
        try:
            async with (
                aiohttp.ClientSession(headers=headers) as session,
                session.ws_connect(self.BASE_API_URL) as ws,
            ):
                await ws.send_bytes(self._pack(start_event))
                await ws.send_bytes(self._pack({"event": "text", "text": text}))
                await ws.send_bytes(self._pack({"event": "stop"}))
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        data = msgpack.unpackb(message.data)
                        event = data.get("event")
                        if event == "audio":
                            await audio_writer.write(data["audio"])
                        elif event == "finish":
                            if data.get("reason") != "stop":
                                raise RuntimeError(
                                    f"{type(self).__name__} API failed: {data.get('reason')}"
                                )
                            break
                        else:
                            _logger.debug(
                                "%s ignored event: %s", type(self).__name__, event
                            )
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ws.exception() or RuntimeError(
                            f"{type(self).__name__} WebSocket error"
                        )
                    else:
                        raise RuntimeError(
                            f"{type(self).__name__} unexpected message type: {message.type}"
                        )
                else:
                    raise RuntimeError(
                        f"{type(self).__name__} WebSocket closed before finish event"
                    )
        except asyncio.CancelledError as error:
            _logger.info("TTS request cancelled: stream=%x", id(audio_writer))
            await audio_writer.write_fail(error)
            raise
        except Exception as error:
            _logger.error(
                "TTS request failed: stream=%x error_type=%s status=%s message=%s",
                id(audio_writer),
                type(error).__name__,
                getattr(error, "status", None),
                getattr(error, "message", None),
            )
            await audio_writer.write_fail(error)
        else:
            await audio_writer.write_eof()

    @staticmethod
    def _pack(payload: dict) -> bytes:
        """Serialize ``payload`` into one MessagePack binary frame.

        Args:
            payload: Event dictionary to serialize.

        Returns:
            MessagePack bytes with string values encoded as strings.
        """
        return cast(bytes, msgpack.packb(payload, use_bin_type=True))


class FishAudioTTSConfig(TTSConfig):
    """Configuration for the Fish Audio live TTS WebSocket endpoint.

    Values are validated on construction against the documented ranges and
    enumerations. Every method that returns a ``dict`` builds a new dictionary
    on each call, so the client may modify it without affecting this
    configuration.

    See https://docs.fish.audio/api-reference/endpoint/websocket/tts-live
    """

    def __init__(
        self,
        voice_name: str,
        *,
        model: str,
        temperature: float = 0.7,
        top_p: float = 0.7,
        reference_id: str | None = None,
        chunk_length: int = 300,
        normalize: bool = True,
        language: str | None = None,
        format: str = "pcm",
        sample_rate: int | None = None,
        mp3_bitrate: int | None = None,
        opus_bitrate: int | None = None,
        latency: str = "normal",
        max_new_tokens: int = 1024,
        repetition_penalty: float = 1.2,
        min_chunk_length: int = 50,
        condition_on_previous_chunks: bool = True,
        early_stop_threshold: float = 1,
    ) -> None:
        """Create a Fish Audio TTS configuration.

        Args:
            voice_name: Name of this configuration shown to users.
            model: Model identifier, for example ``s1`` or ``s2.1-pro``.
            temperature: Sampling temperature in the range 0 to 1.
            top_p: Nucleus sampling probability in the range 0 to 1.
            reference_id: Voice model identifier, or ``None`` for the default
                voice.
            chunk_length: Text segment size in the range 100 to 300.
            normalize: Whether numbers, dates and symbols are spelled out
                before synthesis.
            language: BCP-47 language tag of up to 35 characters, or ``None``
                to detect the language per chunk.
            format: Output format, one of ``wav``, ``pcm``, ``mp3`` or
                ``opus``.
            sample_rate: Output sample rate in Hz, or ``None`` for the format
                default.
            mp3_bitrate: MP3 bitrate in kbps, used when ``format`` is
                ``mp3``.
            opus_bitrate: Opus bitrate in bps, used when ``format`` is
                ``opus``.
            latency: Latency and quality trade-off, ``low``, ``normal`` or
                ``balanced``.
            max_new_tokens: Maximum audio tokens generated per text chunk.
            repetition_penalty: Penalty for repeating audio patterns.
            min_chunk_length: Minimum characters before splitting a new chunk,
                in the range 0 to 100.
            condition_on_previous_chunks: Whether earlier audio is reused as
                context for voice consistency.
            early_stop_threshold: Early stopping threshold in the range 0 to 1.

        Raises:
            ValueError: A value falls outside the documented range or
                enumeration.
        """
        if model not in {
            "s1",
            "s2-pro",
            "s2.1-pro",
            "s2.1-pro-free",
            "drama-3",
            "s3-pro",
        }:
            raise ValueError(f"{type(self).__name__} unknow model: {model}")
        if not 0 <= temperature <= 1:
            raise ValueError(
                f"{type(self).__name__} temperature required range: 0 <= x <= 1, got {temperature}"
            )
        if not 0 <= top_p <= 1:
            raise ValueError(
                f"{type(self).__name__} top_p required range: 0 <= x <= 1, got {top_p}"
            )
        if not 100 <= chunk_length <= 300:
            raise ValueError(
                f"{type(self).__name__} chunk_length required range: 100 <= x <= 300, got {chunk_length}"
            )
        if not 0 <= min_chunk_length <= 100:
            raise ValueError(
                f"{type(self).__name__} min_chunk_length required range: 0 <= x <= 100, got {min_chunk_length}"
            )
        if not 0 <= early_stop_threshold <= 1:
            raise ValueError(
                f"{type(self).__name__} early_stop_threshold required range: 0 <= x <= 1, got {early_stop_threshold}"
            )
        if max_new_tokens <= 0:
            raise ValueError(
                f"{type(self).__name__} max_new_tokens must > 0, got {max_new_tokens}"
            )
        if sample_rate is not None and sample_rate <= 0:
            raise ValueError(
                f"{type(self).__name__} sample_rate must > 0, got {sample_rate}"
            )
        if format not in {"wav", "pcm", "mp3", "opus"}:
            raise ValueError(
                f"{type(self).__name__} fish audio tts api supports only wav, pcm, mp3, opus got {format}"
            )
        if latency not in {"low", "normal", "balanced"}:
            raise ValueError(
                f"{type(self).__name__} latency must be low, normal or balanced, got {latency}"
            )
        if mp3_bitrate is not None and mp3_bitrate not in {64, 128, 192}:
            raise ValueError(
                f"{type(self).__name__} mp3_bitrate must be 64, 128 or 192, got {mp3_bitrate}"
            )
        if opus_bitrate is not None and opus_bitrate not in {
            -1000,
            24000,
            32000,
            48000,
            64000,
        }:
            raise ValueError(
                f"{type(self).__name__} opus_bitrate must be -1000, 24000, 32000, 48000 or 64000, got {opus_bitrate}"
            )
        if language is not None and len(language) > 35:
            raise ValueError(
                f"{type(self).__name__} language must be at most 35 characters"
            )
        self._voice_name = voice_name
        self._model = model
        self._temperature = temperature
        self._top_p = top_p
        self._reference_id = reference_id
        self._chunk_length = chunk_length
        self._normalize = normalize
        self._language = language
        self._format = format
        self._sample_rate = sample_rate
        self._mp3_bitrate = mp3_bitrate
        self._opus_bitrate = opus_bitrate
        self._latency = latency
        self._max_new_tokens = max_new_tokens
        self._repetition_penalty = repetition_penalty
        self._min_chunk_length = min_chunk_length
        self._condition_on_previous_chunks = condition_on_previous_chunks
        self._early_stop_threshold = early_stop_threshold

    def audio_format(self) -> AudioFormat:
        """Return the layout of the audio produced by this configuration.

        The sample rate is the requested one, or the format default of 44100 Hz
        for wav, pcm and mp3 and 48000 Hz for opus. Raw and containerized PCM
        report 16-bit samples; compressed formats leave the sample width unset.
        """
        format_type = AudioFormatType(self._format)
        format_default_sample_rate = {
            AudioFormatType.PCM: 44100,
            AudioFormatType.WAV: 44100,
            AudioFormatType.MP3: 44100,
            AudioFormatType.OPUS: 48000,
        }
        return AudioFormat(
            type=format_type,
            sample_rate=self._sample_rate
            if self._sample_rate is not None
            else format_default_sample_rate[format_type],
            channels=1,
            sample_width=2
            if format_type in {AudioFormatType.PCM, AudioFormatType.WAV}
            else None,
        )

    def voice_name(self) -> str:
        """Return the voice name presented to users."""
        return self._voice_name

    def tts_provider_id(self) -> str:
        """Return the provider identifier this configuration targets."""
        return _TTS_PROVIDER_ID

    def http_headers(self) -> dict:
        """Return the connection headers of this configuration.

        Returns:
            A new dictionary built on every call with the ``model`` header. The
            caller may modify it, for example to add authorization, without
            affecting the configuration or later calls.
        """
        return {"model": self._model}

    def http_params(self) -> dict:
        """The Fish Audio API defines no configurable query parameters here, so
        this position returns an empty dictionary.
        """
        return {}

    def payload_params(self) -> dict:
        """Return the start event fields of this configuration.

        Returns:
            A new dictionary built on every call without ``text``, which the
            client supplies separately. Fields set to ``None`` and bitrates
            that do not apply to the selected format are omitted. The caller
            may modify it without affecting the configuration or later calls.
        """
        params = {
            "format": self._format,
            "sample_rate": self._sample_rate,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "reference_id": self._reference_id,
            "chunk_length": self._chunk_length,
            "normalize": self._normalize,
            "language": self._language,
            "latency": self._latency,
            "max_new_tokens": self._max_new_tokens,
            "repetition_penalty": self._repetition_penalty,
            "min_chunk_length": self._min_chunk_length,
            "condition_on_previous_chunks": self._condition_on_previous_chunks,
            "early_stop_threshold": self._early_stop_threshold,
        }
        if self._format == "mp3" and self._mp3_bitrate is not None:
            params["mp3_bitrate"] = self._mp3_bitrate
        if self._format == "opus" and self._opus_bitrate is not None:
            params["opus_bitrate"] = self._opus_bitrate
        return {key: value for key, value in params.items() if value is not None}
