import asyncio
import threading
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock, patch

from discord import AudioSource, ClientException, Interaction, VoiceClient

from services.voice_manager._internal.guild_playback import GuildPlayback
from services.voice_manager._internal.playback_audio_source import PlaybackAudioSource
from services.voice_manager.errors import VoiceClientError, VoiceManagerQueueFullError
from services.voice_manager.voice_manager import VoiceManager
from services.voice_manager.playback_item import (
    PlaybackItem,
    PlaybackPriority,
    PlaybackSpec,
    PlaybackStatus,
)


class VoiceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = Mock(spec=VoiceClient)
        self.client.channel = object()
        self.client.source = None
        self.started: asyncio.Queue[tuple[PlaybackAudioSource, Callable]] = (
            asyncio.Queue()
        )
        self.client.play.side_effect = self._play
        self.manager = GuildPlayback(1)
        self.addAsyncCleanup(self._shutdown)

    def _play(self, source: PlaybackAudioSource, *, after: Callable) -> None:
        self.client.source = source
        self.started.put_nowait((source, after))

    def _make_task(
        self,
        *,
        priority: PlaybackPriority = PlaybackPriority.NORMAL,
        kind: str = "test",
        failed=None,
        finished=None,
    ):
        audio = Mock(spec=AudioSource)
        audio.is_opus.return_value = False
        interaction = Mock(spec=Interaction)
        interaction.guild_id = 1
        interaction.guild = SimpleNamespace(id=1, voice_client=self.client)
        task = PlaybackItem(
            PlaybackAudioSource(audio, asyncio.get_running_loop()),
            interaction,
            PlaybackSpec(kind=kind, name="test", priority=priority),
            failed_callback=failed,
            finished_callback=finished,
        )
        return task, audio

    async def _shutdown(self) -> None:
        self.manager.close()
        await asyncio.wait_for(self.manager.wait_closed(), 1)

    async def _wait_ready(self, task: PlaybackItem) -> None:
        """Prefetch the source of ``task`` and wait until it is ready."""
        task.preload()
        await asyncio.wait_for(self._poll(task.is_ready), 1)

    async def _poll(self, predicate: Callable[[], bool]) -> None:
        """Wait until ``predicate`` becomes true."""
        while not predicate():
            await asyncio.sleep(0.01)

    async def test_worker_yields_and_plays_serially(self) -> None:
        first, first_audio = self._make_task()
        second, second_audio = self._make_task()
        self.manager.plan(first)
        self.manager.plan(second)
        self.manager.start()

        source, after = await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(source.audio_source, first_audio)
        await asyncio.wait_for(asyncio.sleep(0.01), 1)
        self.assertEqual(self.client.play.call_count, 1)
        self.assertIs(second.status(), PlaybackStatus.WAITING)

        after(None)
        source, _ = await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(source.audio_source, second_audio)
        self.assertIs(first.status(), PlaybackStatus.FINISHED)

    async def test_completion_returns_to_event_loop_thread(self) -> None:
        callback_threads = []
        completed = asyncio.Event()

        def finished(_) -> None:
            callback_threads.append(threading.get_ident())
            completed.set()

        task, audio = self._make_task(finished=finished)
        await self._wait_ready(task)
        task.play()
        _, after = self.started.get_nowait()
        thread = threading.Thread(target=after, args=(None,))
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertIs(task.status(), PlaybackStatus.PLAYING)

        await asyncio.wait_for(completed.wait(), 1)
        self.assertEqual(callback_threads, [threading.get_ident()])
        self.assertIs(task.status(), PlaybackStatus.FINISHED)
        audio.cleanup.assert_called_once()

    async def test_immediate_completion_preserves_finished_state(self) -> None:
        self.client.play.side_effect = lambda source, *, after: after(None)
        task, _ = self._make_task()
        await self._wait_ready(task)
        task.play()
        self.assertIs(task.status(), PlaybackStatus.PLAYING)
        await asyncio.sleep(0)
        self.assertIs(task.status(), PlaybackStatus.FINISHED)

    async def test_playback_error_marks_task_failed(self) -> None:
        failed = Mock()
        task, _ = self._make_task(failed=failed)
        await self._wait_ready(task)
        task.play()
        _, after = self.started.get_nowait()
        error = RuntimeError("playback failed")
        after(error)
        await asyncio.sleep(0)
        self.assertIs(task.status(), PlaybackStatus.FAILED)
        self.assertIsInstance(task.failed_reason(), VoiceClientError)
        failed.assert_called_once_with(task)

    async def test_cancel_playing_stops_only_once_and_ignores_latefinish(self) -> None:
        finished = Mock()
        task, audio = self._make_task(finished=finished)
        await self._wait_ready(task)
        task.play()
        _, after = self.started.get_nowait()
        task.cancel()
        task.cancel()
        self.client.stop.assert_called_once()
        audio.cleanup.assert_called_once()

        after(None)
        await asyncio.sleep(0)
        self.assertIs(task.status(), PlaybackStatus.CANCELLED)
        finished.assert_not_called()

    async def test_cancel_old_task_preserves_another_source(self) -> None:
        task, _ = self._make_task()
        await self._wait_ready(task)
        task.play()
        self.client.source = Mock(spec=AudioSource)
        task.cancel()
        self.client.stop.assert_not_called()

    async def test_cancel_queued_task_releases_source_once(self) -> None:
        task, audio = self._make_task()
        task.cancel()
        task.cancel()
        audio.cleanup.assert_called_once()
        self.assertIs(task.status(), PlaybackStatus.CANCELLED)

    async def test_start_failure_allows_next_task(self) -> None:
        first, first_audio = self._make_task()
        second, second_audio = self._make_task()
        error = ClientException("Not connected to voice.")

        def play(source, *, after) -> None:
            if source.audio_source is first_audio:
                raise error
            self._play(source, after=after)

        self.client.play.side_effect = play
        self.manager.plan(first)
        self.manager.plan(second)
        with self.assertLogs(
            "services.voice_manager._internal.guild_playback", level="ERROR"
        ):
            self.manager.start()
            source, _ = await asyncio.wait_for(self.started.get(), 1)

        self.assertIs(source.audio_source, second_audio)
        self.assertIs(first.status(), PlaybackStatus.FAILED)
        self.assertIs(first.failed_reason(), error)
        first_audio.cleanup.assert_called_once()
        self.assertIs(second.status(), PlaybackStatus.PLAYING)

    async def test_failed_callback_error_allows_next_task_and_cleanup(self) -> None:
        callback = Mock(side_effect=RuntimeError("callback failed"))
        first, first_audio = self._make_task(failed=callback)
        second, second_audio = self._make_task()

        def play(source, *, after) -> None:
            if source.audio_source is first_audio:
                raise ClientException("Not connected to voice.")
            self._play(source, after=after)

        self.client.play.side_effect = play
        self.manager.plan(first)
        self.manager.plan(second)
        with self.assertLogs(
            "services.voice_manager._internal.guild_playback", level="ERROR"
        ):
            self.manager.start()
            source, _ = await asyncio.wait_for(self.started.get(), 1)

        self.assertIs(source.audio_source, second_audio)
        callback.assert_called_once_with(first)
        first_audio.cleanup.assert_called_once()

    async def test_duplicate_start_reuses_worker(self) -> None:
        self.manager.start()
        worker = self.manager._task
        self.manager.start()
        self.assertIs(self.manager._task, worker)

    async def test_close_before_start_is_idempotent(self) -> None:
        self.manager.close()
        self.manager.close()
        await self.manager.wait_closed()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.manager.start()

    async def test_close_stops_current_and_cancels_queue(self) -> None:
        current, current_audio = self._make_task()
        queued, queued_audio = self._make_task()
        self.manager.plan(current)
        self.manager.plan(queued)
        self.manager.start()
        await asyncio.wait_for(self.started.get(), 1)

        self.manager.close()
        await asyncio.wait_for(self.manager.wait_closed(), 1)
        await asyncio.wait_for(self.manager._queue.join(), 1)
        self.assertIs(current.status(), PlaybackStatus.CANCELLED)
        self.assertIs(queued.status(), PlaybackStatus.CANCELLED)
        self.client.stop.assert_called_once()
        current_audio.cleanup.assert_called_once()
        queued_audio.cleanup.assert_called_once()
        self.assertFalse(self.manager._running)
        self.assertTrue(self.manager._queue.empty())

    async def test_closed_manager_rejects_and_cleans_new_task(self) -> None:
        self.manager.close()
        failed = Mock()
        task, audio = self._make_task(failed=failed)
        self.manager.plan(task)
        self.assertIs(task.status(), PlaybackStatus.FAILED)
        self.assertIsInstance(task.failed_reason(), RuntimeError)
        failed.assert_called_once_with(task)
        audio.cleanup.assert_called_once()
        self.assertTrue(self.manager._queue.empty())

    async def test_queue_full_fails_and_cleans_rejected_task(self) -> None:
        for _ in range(20):
            self.manager.plan(self._make_task()[0])
        task, audio = self._make_task()
        self.manager.plan(task)
        self.assertIs(task.status(), PlaybackStatus.FAILED)
        self.assertIsInstance(task.failed_reason(), VoiceManagerQueueFullError)
        audio.cleanup.assert_called_once()

    async def test_cleanup_failure_keeps_closing_other_tasks(self) -> None:
        first, first_audio = self._make_task()
        second, second_audio = self._make_task()
        first_audio.cleanup.side_effect = RuntimeError("cleanup failed")
        self.manager.plan(first)
        self.manager.plan(second)
        with self.assertLogs(
            "services.voice_manager._internal.guild_playback", level="ERROR"
        ):
            self.manager.close()
        self.assertIs(second.status(), PlaybackStatus.CANCELLED)
        second_audio.cleanup.assert_called_once()
        await asyncio.wait_for(self.manager._queue.join(), 1)

    async def test_high_priority_preempts_and_resumes_same_source(self) -> None:
        music, music_audio = self._make_task(priority=PlaybackPriority.LOW)
        speech, speech_audio = self._make_task(priority=PlaybackPriority.HIGH)
        self.manager.plan(music)
        self.manager.start()
        first_source, music_after = await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(first_source.audio_source, music_audio)
        self.assertIs(music.status(), PlaybackStatus.PLAYING)

        self.manager.plan(speech)
        await asyncio.wait_for(
            self._poll(lambda: music.status() is PlaybackStatus.INTERRUPTED), 1
        )
        self.assertEqual(first_source.read(), b"")
        speech_source, speech_after = await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(speech_source.audio_source, speech_audio)
        self.assertIs(speech.status(), PlaybackStatus.PLAYING)

        music_after(None)
        await asyncio.sleep(0.01)
        self.assertIs(music.status(), PlaybackStatus.INTERRUPTED)

        speech_after(None)
        resumed_source, resumed_after = await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(resumed_source, first_source)
        self.assertIs(resumed_source.audio_source, music_audio)
        self.assertIs(music.status(), PlaybackStatus.PLAYING)
        music_audio.cleanup.assert_not_called()

        resumed_after(None)
        await asyncio.sleep(0.01)
        self.assertIs(music.status(), PlaybackStatus.FINISHED)
        music_audio.cleanup.assert_called_once()

    async def test_low_priority_item_is_not_preloaded_before_dequeue(self) -> None:
        current, _ = self._make_task(priority=PlaybackPriority.NORMAL)
        queued, queued_audio = self._make_task(priority=PlaybackPriority.LOW)
        self.manager.plan(current)
        self.manager.start()
        await asyncio.wait_for(self.started.get(), 1)

        self.manager.plan(queued)
        await asyncio.sleep(0.05)

        self.assertFalse(queued.is_ready())
        queued_audio.read.assert_not_called()

    async def test_ready_callback_reports_the_prefetched_chunk(self) -> None:
        task, _ = self._make_task()
        called = list[BaseException | None]()
        task.set_ready_callback(called.append)

        task.preload()
        await asyncio.wait_for(self._poll(task.is_ready), 1)
        await asyncio.sleep(0)

        self.assertEqual(called, [None])

    async def test_prefetch_failure_fails_the_item(self) -> None:
        failed = Mock()
        task, audio = self._make_task(priority=PlaybackPriority.HIGH, failed=failed)
        audio.read.side_effect = RuntimeError("upstream failed")

        self.manager.plan(task)
        await asyncio.wait_for(
            self._poll(lambda: task.status() is PlaybackStatus.FAILED), 1
        )

        self.assertIsInstance(task.failed_reason(), RuntimeError)
        failed.assert_called_once_with(task)
        audio.cleanup.assert_called_once()

    async def test_unready_item_is_requeued_after_the_timeout(self) -> None:
        blocked = threading.Event()
        stuck, stuck_audio = self._make_task(priority=PlaybackPriority.HIGH)
        stuck_audio.read.side_effect = lambda: blocked.wait(5) and b"\x01\x00"
        self.manager.READINESS_TIMEOUT = 0.05

        with self.assertLogs(
            "services.voice_manager._internal.guild_playback", level="WARNING"
        ) as logs:
            self.manager.plan(stuck)
            self.manager.start()
            await asyncio.wait_for(self._poll(lambda: bool(logs.output)), 1)

        self.assertIn("not ready, requeued", logs.output[0])
        self.assertIs(stuck.status(), PlaybackStatus.WAITING)
        self.client.play.assert_not_called()

        blocked.set()
        await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(stuck.status(), PlaybackStatus.PLAYING)

    async def test_equal_priority_does_not_preempt(self) -> None:
        first, _ = self._make_task()
        second, _ = self._make_task()
        self.manager.plan(first)
        self.manager.start()
        await asyncio.wait_for(self.started.get(), 1)

        self.manager.plan(second)
        self.assertIs(first.status(), PlaybackStatus.PLAYING)
        self.client.stop.assert_not_called()

    async def test_preemption_fails_item_when_requeue_cannot_fit(self) -> None:
        current, current_audio = self._make_task(priority=PlaybackPriority.LOW)
        self.manager.plan(current)
        self.manager.start()
        await asyncio.wait_for(self.started.get(), 1)
        for _ in range(19):
            self.manager.plan(self._make_task(priority=PlaybackPriority.LOW)[0])

        self.manager.plan(self._make_task(priority=PlaybackPriority.HIGH)[0])

        await asyncio.wait_for(
            self._poll(lambda: current.status() is PlaybackStatus.FAILED), 1
        )
        self.assertIs(current.status(), PlaybackStatus.FAILED)
        self.assertIsInstance(current.failed_reason(), VoiceManagerQueueFullError)
        current_audio.cleanup.assert_called_once()

    async def test_close_cancels_interrupted_item_waiting_for_resume(self) -> None:
        music, music_audio = self._make_task(priority=PlaybackPriority.LOW)
        speech, _ = self._make_task(priority=PlaybackPriority.HIGH)
        self.manager.plan(music)
        self.manager.start()
        await asyncio.wait_for(self.started.get(), 1)

        self.manager.plan(speech)
        await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(music.status(), PlaybackStatus.INTERRUPTED)

        self.manager.close()
        await asyncio.wait_for(self.manager.wait_closed(), 1)
        await asyncio.wait_for(self.manager._queue.join(), 1)

        self.assertIs(music.status(), PlaybackStatus.CANCELLED)
        music_audio.cleanup.assert_called_once()

    async def test_cancelling_preempting_item_resumes_interrupted_item(self) -> None:
        music, music_audio = self._make_task(priority=PlaybackPriority.LOW)
        speech, _ = self._make_task(priority=PlaybackPriority.HIGH)
        self.manager.plan(music)
        self.manager.start()
        await asyncio.wait_for(self.started.get(), 1)

        self.manager.plan(speech)
        await asyncio.wait_for(self.started.get(), 1)
        self.assertIs(music.status(), PlaybackStatus.INTERRUPTED)

        self.assertTrue(self.manager.cancel())
        resumed, _ = await asyncio.wait_for(self.started.get(), 1)

        self.assertIs(resumed.audio_source, music_audio)
        self.assertIs(music.status(), PlaybackStatus.PLAYING)
        self.assertIs(speech.status(), PlaybackStatus.CANCELLED)

    async def test_preemption_keeps_queue_join_balanced(self) -> None:
        music, _ = self._make_task(priority=PlaybackPriority.LOW)
        speech, _ = self._make_task(priority=PlaybackPriority.HIGH)
        self.manager.plan(music)
        self.manager.start()
        await asyncio.wait_for(self.started.get(), 1)

        self.manager.plan(speech)
        _, speech_after = await asyncio.wait_for(self.started.get(), 1)
        speech_after(None)
        _, resumed_after = await asyncio.wait_for(self.started.get(), 1)
        resumed_after(None)

        await asyncio.wait_for(self.manager._queue.join(), 1)
        self.assertIs(speech.status(), PlaybackStatus.FINISHED)
        self.assertIs(music.status(), PlaybackStatus.FINISHED)

    def test_spec_rejects_an_empty_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "kind must not be empty"):
            PlaybackSpec(kind="", name="test", priority=PlaybackPriority.NORMAL)
        with self.assertRaisesRegex(ValueError, "kind must not be empty"):
            PlaybackSpec(kind="   ", name="test", priority=PlaybackPriority.NORMAL)

    def test_spec_rejects_an_empty_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "name must not be empty"):
            PlaybackSpec(kind="test", name="", priority=PlaybackPriority.NORMAL)
        with self.assertRaisesRegex(ValueError, "name must not be empty"):
            PlaybackSpec(kind="test", name="   ", priority=PlaybackPriority.NORMAL)

    async def test_reading_is_blocked_between_stop_and_resume(self) -> None:
        upstream = Mock(spec=AudioSource)
        upstream.is_opus.return_value = False
        chunk = b"\x01\x00\x02\x00"
        upstream.read.return_value = chunk

        source = PlaybackAudioSource(upstream, asyncio.get_running_loop())
        source.preload()
        while not source.is_ready():
            await asyncio.sleep(0.01)

        self.assertEqual(source.read(), chunk)
        source.stop_reading()
        self.assertEqual(source.read(), b"")
        source.resume_reading()
        self.assertEqual(source.read(), chunk)
        self.assertEqual(upstream.read.call_count, 2)

    async def test_terminal_results_and_callbacks_are_preserved(self) -> None:
        failed = Mock()
        finished = Mock()
        task, audio = self._make_task(failed=failed, finished=finished)
        error = RuntimeError("first failure")
        task.fail(error)
        with self.assertRaisesRegex(RuntimeError, "fail is only supported"):
            task.fail(RuntimeError("later failure"))
        with self.assertRaisesRegex(RuntimeError, "finish is only supported"):
            task.finish()
        task.cancel()
        self.assertIs(task.status(), PlaybackStatus.FAILED)
        self.assertIs(task.failed_reason(), error)
        failed.assert_called_once_with(task)
        finished.assert_not_called()
        audio.cleanup.assert_called_once()


class VoiceManagerSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = VoiceManager()
        self.guild_manager = GuildPlayback(1)
        self.manager._guild_playbacks[1] = self.guild_manager
        self.addCleanup(self.manager.close)
        self.source = Mock(spec=AudioSource)
        self.source.is_opus.return_value = False
        client = Mock(spec=VoiceClient)
        client.channel = object()
        self.interaction = Mock(spec=Interaction)
        self.interaction.guild_id = 1
        self.interaction.guild = SimpleNamespace(id=1, voice_client=client)
        self.spec = PlaybackSpec(
            kind="test", name="test", priority=PlaybackPriority.NORMAL
        )

    async def test_full_queue_raises_before_task_construction(self) -> None:
        for index in range(20):
            self.guild_manager._queue.put_nowait((index, index, Mock()))
        failed = Mock()
        finished = Mock()
        with patch("services.voice_manager.voice_manager.PlaybackItem") as constructor:
            with self.assertRaises(VoiceManagerQueueFullError):
                self.manager.plan(
                    self.source,
                    self.interaction,
                    self.spec,
                    failed_callback=failed,
                    finished_callback=finished,
                )
        constructor.assert_not_called()
        failed.assert_not_called()
        finished.assert_not_called()
        self.source.cleanup.assert_not_called()
        self.assertEqual(self.guild_manager._queue.qsize(), 20)

    async def test_available_queue_constructs_and_returns_task(self) -> None:
        with patch(
            "services.voice_manager.voice_manager.PlaybackItem",
            wraps=PlaybackItem,
        ) as constructor:
            task = self.manager.plan(self.source, self.interaction, self.spec)
        constructor.assert_called_once()
        self.assertIs(task.status(), PlaybackStatus.WAITING)
        self.assertEqual(self.guild_manager._queue.qsize(), 1)
        self.source.cleanup.assert_not_called()

    async def test_freed_capacity_accepts_new_task(self) -> None:
        for index in range(20):
            self.guild_manager._queue.put_nowait((index, index, Mock()))
        self.guild_manager._queue.get_nowait()
        self.guild_manager._queue.task_done()
        task = self.manager.plan(self.source, self.interaction, self.spec)
        self.assertIs(task.status(), PlaybackStatus.WAITING)
        self.assertEqual(self.guild_manager._queue.qsize(), 20)

    async def test_first_submission_creates_and_starts_guild_manager(self) -> None:
        self.manager._guild_playbacks.clear()
        with patch.object(GuildPlayback, "start") as start:
            task = self.manager.plan(self.source, self.interaction, self.spec)
        start.assert_called_once()
        guild_manager = self.manager._guild_playbacks[1]
        self.assertEqual(guild_manager._queue.qsize(), 1)
        self.assertIs(task.status(), PlaybackStatus.WAITING)


if __name__ == "__main__":
    unittest.main()
