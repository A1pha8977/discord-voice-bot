import asyncio
import io
import logging
import struct
import threading
from concurrent.futures import Future

from discord import AudioSource, FFmpegPCMAudio

from utils.audio.audio_format import AudioFormatType
from utils.audio.audio_reader import AudioReader

_logger = logging.getLogger(__name__)


class _AudioReaderPipe(io.BufferedIOBase):
    def __init__(self, reader: AudioReader, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._reader = reader
        self._loop = loop
        self._lock = threading.Lock()
        self._pending: Future[bytes] | None = None
        self._close_future: Future[None] | None = None
        self.error: Exception | None = None
        self.bytes_read = 0

    def read(self, size: int | None = -1) -> bytes:
        with self._lock:
            if self.closed:
                return b""
            future = asyncio.run_coroutine_threadsafe(
                self._reader.read(None if size is None or size < 0 else size),
                self._loop,
            )
            self._pending = future
        try:
            data = future.result()
            self.bytes_read += len(data)
            return data
        except Exception as error:
            with self._lock:
                if not self.closed:
                    self.error = error
            return b""
        finally:
            with self._lock:
                self._pending = None

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            super().close()
            if self._pending is not None:
                self._pending.cancel()
            if not self._loop.is_closed():
                self._close_future = asyncio.run_coroutine_threadsafe(
                    self._reader.read_close(), self._loop
                )


class PCMAudioReaderSource(AudioSource):
    def __init__(self, reader: AudioReader) -> None:
        self._audio: FFmpegPCMAudio | None = None
        self._reader_pipe: _AudioReaderPipe | None = None
        self._loop_thread = threading.get_ident()
        self._stream_id = id(reader)
        self._frames = 0
        self._pcm_bytes = 0
        self._peak = 0
        audio_format = reader.format()
        if audio_format.type != AudioFormatType.PCM:
            raise ValueError("PCMAudioReaderSource requires PCM input")
        if audio_format.sample_width != 2:
            raise ValueError("PCMAudioReaderSource requires 16-bit little-endian PCM")
        if audio_format.sample_rate is None or audio_format.sample_rate <= 0:
            raise ValueError("PCM input requires a positive sample rate")
        if audio_format.channels is None or audio_format.channels <= 0:
            raise ValueError("PCM input requires a positive channel count")
        loop = asyncio.get_running_loop()
        self._reader_pipe = _AudioReaderPipe(reader, loop)
        try:
            self._audio = FFmpegPCMAudio(
                self._reader_pipe,
                pipe=True,
                before_options=(
                    f"-f s16le -ar {audio_format.sample_rate} "
                    f"-ac {audio_format.channels}"
                ),
            )
        except Exception:
            self.cleanup()
            raise
        _logger.info(
            "TTS PCM source created: stream=%x sample_rate=%s channels=%s sample_width=%s",
            self._stream_id,
            audio_format.sample_rate,
            audio_format.channels,
            audio_format.sample_width,
        )

    def read(self) -> bytes:
        pipe = self._reader_pipe
        audio = self._audio
        if pipe is None or pipe.closed or audio is None:
            return b""
        if threading.get_ident() == self._loop_thread:
            raise RuntimeError("AudioReaderSource.read() requires a playback thread")
        data = audio.read()
        if pipe.error is not None:
            _logger.error(
                "TTS PCM read failed: stream=%x error_type=%s cause_type=%s",
                self._stream_id,
                type(pipe.error).__name__,
                type(pipe.error.__cause__).__name__,
            )
            raise pipe.error
        if data:
            self._frames += 1
            self._pcm_bytes += len(data)
            self._peak = max(
                self._peak,
                max(
                    (abs(sample) for (sample,) in struct.iter_unpack("<h", data)),
                    default=0,
                ),
            )
            if self._frames == 1:
                _logger.debug(
                    "TTS first PCM frame: stream=%x bytes=%s peak=%s",
                    self._stream_id,
                    len(data),
                    self._peak,
                )
        else:
            _logger.debug(
                "TTS PCM EOF: stream=%x input_bytes=%s frames=%s output_bytes=%s peak=%s",
                self._stream_id,
                pipe.bytes_read,
                self._frames,
                self._pcm_bytes,
                self._peak,
            )
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        if self._reader_pipe is not None:
            self._reader_pipe.close()
        audio = self._audio
        self._audio = None
        if audio is not None:
            _logger.debug(
                "TTS PCM cleanup: stream=%x input_bytes=%s frames=%s output_bytes=%s peak=%s",
                self._stream_id,
                self._reader_pipe.bytes_read if self._reader_pipe is not None else 0,
                self._frames,
                self._pcm_bytes,
                self._peak,
            )
            audio.cleanup()
