import asyncio
import logging

import aiohttp

from services.tts.tts_client import TTSClient
from services.tts.tts_config import TTSConfig
from utils.audio.audio_format import AudioFormat, AudioFormatType
from utils.audio.audio_reader import AudioReader
from utils.audio.audio_stream import AudioStream
from utils.audio.audio_writer import AudioWriter

_logger = logging.getLogger(__name__)
_TTS_PROVIDER_ID = "DEEPSEEK"


class DeepseekTTSClient(TTSClient):
    """Stream speech from the DeepSeek WebSocket TTS endpoint.

    One request opens a WebSocket connection, sends the text, forwards the
    received audio frames and closes the stream when the server reports
    completion. Production failures are delivered to the reader instead of
    being raised from ``generate()``.
    """

    BASE_URL: str = r"wss://api.deepseek.com/v1/tools/tts"

    def __init__(self, api_key: str):
        """Create a client bound to one DeepSeek API key.

        Args:
            api_key: DeepSeek API key used as a bearer token.
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
            config: DeepSeek configuration. Its provider identifier must be
                ``DEEPSEEK`` and its HTTP parameters become the WebSocket query
                parameters.

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
                f"{type(self).__name__}: voice_setting.tts_provider_id must be {_TTS_PROVIDER_ID}"
            )
        loop = asyncio.get_running_loop()
        audio_stream = AudioStream(config.audio_format())
        task = loop.create_task(
            self._call_api_then_write_back(
                text,
                config.http_params(),
                audio_stream,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return audio_stream

    async def _call_api_then_write_back(
        self, text: str, voice_setting: dict, audio_writer: AudioWriter
    ) -> None:
        """Stream one request into ``audio_writer``.

        Args:
            text: Text to synthesize.
            voice_setting: Query parameters taken from the configuration.
                Boolean values are sent lowercase.
            audio_writer: Producer side of the audio stream that receives the
                audio frames.

        Note:
            Ends the stream with ``write_eof()`` on success and ``write_fail()``
            with the original error on failure or cancellation. Errors are
            recorded in the log and are not raised to the caller.
        """
        try:
            headers = {"Authorization": f"Bearer {self._api_key}"}
            params = {
                key: str(value).lower() if isinstance(value, bool) else value
                for key, value in voice_setting.items()
            }
            async with (
                aiohttp.ClientSession(headers=headers) as session,
                session.ws_connect(self.BASE_URL, params=params) as ws,
            ):
                await ws.send_json({"event": "text", "text": text})
                await ws.send_json({"event": "finish_text"})
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        await audio_writer.write(message.data)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        message_json = message.json()
                        if message_json["event"] != "finish":
                            raise RuntimeError(
                                f"{type(self).__name__} unknown event: {message_json['event']}"
                            )
                        code = message_json["code"]
                        if code != "success":
                            raise RuntimeError(
                                f"{type(self).__name__} API failed ({code}): "
                                f"{message_json.get('message', '')}"
                            )
                        break
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ws.exception() or RuntimeError(
                            f"{type(self).__name__} WebSocket error"
                        )
                    else:
                        raise RuntimeError(
                            f"{type(self).__name__} unknown WebSocket message type: {message.type}"
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
                "TTS request failed: stream=%x error_type=%s status=%s",
                id(audio_writer),
                type(error).__name__,
                getattr(error, "status", None),
            )
            _logger.error(f"Error: {error}")
            await audio_writer.write_fail(error)
        else:
            await audio_writer.write_eof()


class DeepseekTTSConfig(TTSConfig):
    """Configuration for the DeepSeek WebSocket TTS endpoint.

    The endpoint accepts the ``pcm`` and ``opus`` output formats. Every method
    that returns a ``dict`` builds a new dictionary on each call, so the client
    may modify it without affecting this configuration.
    """

    def __init__(
        self,
        voice_name: str,
        *,
        audio_format_type: str = "pcm",
        voice_id: str = "stella",
        filter_markdown: bool = True,
    ) -> None:
        """Create a DeepSeek TTS configuration.

        Args:
            voice_name: Name of this configuration shown to users.
            audio_format_type: Output format, ``pcm`` or ``opus``.
            voice_id: Provider voice identifier, for example ``stella`` or
                ``tide``.
            filter_markdown: Whether the provider removes Markdown before
                reading the text.

        Raises:
            ValueError: ``audio_format_type`` is not ``pcm`` or ``opus``.
        """
        if audio_format_type is not None and audio_format_type.lower() not in {
            "opus",
            "pcm",
        }:
            raise ValueError("DeepSeek WebSocket TTS supports only PCM and Opus")
        self._voice_name = voice_name
        self._audio_format_type = audio_format_type.lower()
        self._voice_id = voice_id
        self._filter_markdown = filter_markdown

    def tts_provider_id(self) -> str:
        """Return the provider identifier this configuration targets."""
        return _TTS_PROVIDER_ID

    def audio_format(self) -> AudioFormat:
        """Return the layout of the audio produced by this configuration.

        PCM describes 24 kHz, 16-bit mono audio. Opus is packet based, so its
        rate, channel and sample-width fields stay unset.
        """
        audio_format_args = {
            "type": AudioFormatType(self._audio_format_type),
            "sample_rate": None,
            "channels": None,
            "sample_width": None,
        }
        if self._audio_format_type == "pcm":
            audio_format_args["sample_rate"] = 24000
            audio_format_args["channels"] = 1
            audio_format_args["sample_width"] = 2
        return AudioFormat(**audio_format_args)

    def voice_name(self) -> str:
        """Return the voice name presented to users."""
        return self._voice_name

    def http_params(self) -> dict:
        """Return the WebSocket query parameters of this configuration.

        Returns:
            A new dictionary built on every call with ``format``, ``voice_id``
            and ``filter_markdown``. The caller may modify it without affecting
            the configuration or later calls.
        """
        return {
            "format": self._audio_format_type,
            "voice_id": self._voice_id,
            "filter_markdown": self._filter_markdown,
        }

    def http_headers(self) -> dict:
        """The DeepSeek API defines no configurable HTTP header parameters
        here, so this position returns an empty dictionary.
        """
        return {}

    def payload_params(self) -> dict:
        """The DeepSeek API defines no configurable request payload parameters
        here, so this position returns an empty dictionary.
        """
        return {}
