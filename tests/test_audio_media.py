import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, mock_open, patch

from discord import AudioSource, ClientException, Interaction, Member, VoiceClient

from cogs.audio_media.audio_media_cog import AudioMediaCog
from services.voice_manager._internal.guild_playback import GuildPlayback
from services.voice_manager.errors import VoiceManagerQueueFullError
from services.voice_manager.voice_manager import VoiceManager
from services.voice_manager.playback_item import PlaybackItem, PlaybackStatus


class AudioMediaAutocompleteTests(unittest.IsolatedAsyncioTestCase):
    async def _complete(self, names: list[str], current: str):
        with patch.object(AudioMediaCog, "_discover_songs"):
            cog = AudioMediaCog(Mock(), Path("."))
        cog._songs.update({name: Path(name) for name in names})
        callback = cog.play._params["sound_name"].autocomplete
        assert callback is not None
        return await callback(cog, Mock(spec=Interaction), current)

    async def test_matches_prefix_ignoring_case(self) -> None:
        choices = await self._complete(
            ["HirO-line", "Void Annihilation", "long-day"], "hIRO"
        )
        self.assertEqual([choice.value for choice in choices], ["HirO-line"])

    async def test_substring_alone_does_not_match(self) -> None:
        choices = await self._complete(["long-day", "holiday"], "day")
        self.assertEqual(choices, [])

    async def test_preserves_original_names_and_values(self) -> None:
        name = "HirO-\u7dda-"
        choices = await self._complete([name], "hiro-")
        self.assertEqual(
            [(choice.name, choice.value) for choice in choices], [(name, name)]
        )

    async def test_empty_prefix_returns_stably_sorted_names(self) -> None:
        choices = await self._complete(["beta", "alpha", "Alpha"], "")
        self.assertEqual(
            [choice.value for choice in choices], ["Alpha", "alpha", "beta"]
        )

    async def test_limits_results_to_25(self) -> None:
        names = [f"song-{index:02d}" for index in reversed(range(30))]
        choices = await self._complete(names, "SONG-")
        self.assertEqual(
            [choice.value for choice in choices],
            [f"song-{index:02d}" for index in range(25)],
        )

    async def test_casefold_handles_unicode_case_equivalence(self) -> None:
        choices = await self._complete(["Stra\u00dfe"], "STRASS")
        self.assertEqual([choice.value for choice in choices], ["Stra\u00dfe"])

    async def test_empty_library_returns_no_choices(self) -> None:
        self.assertEqual(await self._complete([], "song"), [])


class AudioMediaCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = Mock(spec=VoiceManager)
        self.task = Mock(spec=PlaybackItem)
        self.task.status.return_value = PlaybackStatus.WAITING
        self.manager.plan.return_value = self.task
        with patch.object(AudioMediaCog, "_discover_songs"):
            self.cog = AudioMediaCog(self.manager, Path("songs"))
        self.cog._songs["test"] = Path("songs/test.mp3")
        self.source = Mock(spec=AudioSource)
        self.source.is_opus.return_value = False
        patcher = patch(
            "cogs.audio_media.audio_media_cog.FFmpegPCMAudio",
            return_value=self.source,
        )
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        client = Mock(spec=VoiceClient)
        client.channel = object()
        self.interaction = Mock(spec=Interaction)
        self.interaction.channel = object()
        self.interaction.user = Mock(spec=Member)
        self.interaction.guild_id = 1
        self.interaction.guild = SimpleNamespace(id=1, voice_client=client)
        self.interaction.response = SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock()
        )
        self.interaction.edit_original_response = AsyncMock()

    async def _play(self) -> None:
        callback = cast(
            Callable[[AudioMediaCog, Interaction, str], Awaitable[None]],
            self.cog.play.callback,
        )
        await callback(self.cog, self.interaction, "test")

    async def _reload(self) -> None:
        callback = cast(
            Callable[[AudioMediaCog, Interaction], Awaitable[None]],
            self.cog.reload_songs.callback,
        )
        await callback(self.cog, self.interaction)

    async def test_success_reports_planned_and_transfers_source(self) -> None:
        await self._play()
        self.assertEqual(self.factory.call_args.args, ("songs/test.mp3",))
        self.manager.plan.assert_called_once()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="Audio planned"
        )
        self.source.cleanup.assert_not_called()

    async def test_queue_full_exception_cleans_source_and_reports_failure(self) -> None:
        self.manager.plan.side_effect = VoiceManagerQueueFullError()
        await self._play()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="The audio queue is full."
        )
        self.source.cleanup.assert_called_once()
        self.task.status.assert_not_called()

    async def test_other_rejection_reports_failure(self) -> None:
        self.task.status.return_value = PlaybackStatus.FAILED
        self.task.failed_reason.return_value = RuntimeError("manager closed")
        await self._play()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="Failed to plan audio."
        )
        self.source.cleanup.assert_not_called()

    async def test_full_queue_cleans_source_once_and_reports_failure(self) -> None:
        manager = VoiceManager()
        guild = GuildPlayback(1)
        manager._guild_playbacks[1] = guild
        self.addCleanup(manager.close)
        for index in range(20):
            guild._queue.put_nowait((index, index, Mock()))
        self.cog._voice_manager = manager
        with patch("services.voice_manager.voice_manager.PlaybackItem") as constructor:
            await self._play()
        constructor.assert_not_called()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="The audio queue is full."
        )
        self.source.cleanup.assert_called_once()

    async def test_source_creation_error_updates_deferred_response(self) -> None:
        self.factory.side_effect = ClientException("ffmpeg unavailable")
        with self.assertLogs("cogs.audio_media.audio_media_cog", level="ERROR"):
            await self._play()
        self.interaction.response.defer.assert_awaited_once()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="Failed to plan audio."
        )
        self.manager.plan.assert_not_called()
        self.source.cleanup.assert_not_called()

    async def test_submission_error_cleans_source_and_reports_failure(self) -> None:
        self.manager.plan.side_effect = ValueError("disconnected")
        with self.assertLogs("cogs.audio_media.audio_media_cog", level="ERROR"):
            await self._play()
        self.source.cleanup.assert_called_once()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="Failed to plan audio."
        )

    async def test_disconnect_during_defer_cleans_unsubmitted_source(self) -> None:
        self.cog._voice_manager = VoiceManager()

        async def disconnect(**kwargs) -> None:
            self.interaction.guild.voice_client = None

        self.interaction.response.defer.side_effect = disconnect
        with self.assertLogs("cogs.audio_media.audio_media_cog", level="ERROR"):
            await self._play()
        self.source.cleanup.assert_called_once()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="Failed to plan audio."
        )

    async def test_cleanup_error_still_updates_deferred_response(self) -> None:
        self.manager.plan.side_effect = ValueError("disconnected")
        self.source.cleanup.side_effect = OSError("cleanup failed")
        with self.assertLogs("cogs.audio_media.audio_media_cog", level="ERROR"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                await self._play()
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="Failed to plan audio."
        )

    async def test_reply_error_preserves_submitted_source(self) -> None:
        self.interaction.edit_original_response.side_effect = RuntimeError(
            "response unavailable"
        )
        with self.assertRaisesRegex(RuntimeError, "response unavailable"):
            await self._play()
        self.manager.plan.assert_called_once()
        self.source.cleanup.assert_not_called()

    async def test_reload_during_defer_preserves_selected_path(self) -> None:
        async def reload_catalog(**kwargs) -> None:
            self.cog._songs.clear()

        self.interaction.response.defer.side_effect = reload_catalog
        await self._play()
        self.assertEqual(self.factory.call_args.args, ("songs/test.mp3",))
        self.interaction.edit_original_response.assert_awaited_once_with(
            content="Audio planned"
        )

    async def test_reload_replaces_deleted_names(self) -> None:
        new_path = Path("songs/fresh.mp3")
        with (
            patch.object(Path, "iterdir", return_value=iter([new_path])),
            patch.object(Path, "is_file", return_value=True),
            patch("builtins.open", mock_open()),
        ):
            await self._reload()
        self.assertEqual(self.cog._songs, {"fresh": new_path})
        self.interaction.response.send_message.assert_awaited_once_with(
            "songs reloaded", ephemeral=True
        )

    async def test_empty_directory_clears_catalog(self) -> None:
        with patch.object(Path, "iterdir", return_value=iter(())):
            self.cog._discover_songs()
        self.assertEqual(self.cog._songs, {})

    async def test_unreadable_song_is_removed_from_catalog(self) -> None:
        with (
            patch.object(Path, "iterdir", return_value=iter([Path("songs/test.mp3")])),
            patch.object(Path, "is_file", return_value=True),
            patch("builtins.open", side_effect=PermissionError("unreadable")),
            self.assertLogs("cogs.audio_media.audio_media_cog", level="ERROR"),
        ):
            self.cog._discover_songs()
        self.assertEqual(self.cog._songs, {})

    async def test_reload_error_preserves_catalog_and_reports_failure(self) -> None:
        previous = self.cog._songs
        with (
            patch.object(Path, "iterdir", side_effect=OSError("scan failed")),
            self.assertLogs("cogs.audio_media.audio_media_cog", level="ERROR"),
        ):
            await self._reload()
        self.assertIs(self.cog._songs, previous)
        self.interaction.response.send_message.assert_awaited_once_with(
            "Failed to reload songs.", ephemeral=True
        )

    async def test_partial_scan_error_preserves_entire_previous_catalog(self) -> None:
        def entries():
            yield Path("songs/fresh.mp3")
            raise OSError("scan interrupted")

        previous = self.cog._songs
        with (
            patch.object(Path, "iterdir", return_value=entries()),
            patch.object(Path, "is_file", return_value=True),
            patch("builtins.open", mock_open()),
            self.assertRaisesRegex(OSError, "scan interrupted"),
        ):
            self.cog._discover_songs()
        self.assertIs(self.cog._songs, previous)
        self.assertEqual(self.cog._songs.names(), {"test"})


if __name__ == "__main__":
    unittest.main()
