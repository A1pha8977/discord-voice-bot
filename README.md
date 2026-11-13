中文版：[README.zh.md](README.zh.md)

# my-tts-bot

my-tts-bot is a bot for Discord voice channels. It plays local audio files and provides multi-voice text-to-speech (TTS) through DeepSeek and Fish Audio.

## Deployment

Requires Python 3.12+, FFmpeg, and a Discord bot token invited with the `bot` and `applications.commands` scopes plus Connect, Speak and Send Messages permissions.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Create `.env` and the files under `config/` before starting; see Configuration.

Startup flags:

| Flag | Default | Description |
|---|---|---|
| `--log-level` | `INFO` | Root log level: `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `--debug-module NAME` | none | Set one logger to `DEBUG`; repeatable, e.g. `--debug-module services.tts.fish_audio_tts_client` |

Logs go to both the console and `logs/bot.log`, rotated daily with all archives kept.

## Configuration

`.env` and `config/` are ignored by `.gitignore` and must be created manually.

### Environment variables (`.env`)

```dotenv
DISCORD_BOT_TOKEN=your-discord-bot-token
DEEPSEEK_API_KEY=your-deepseek-api-key
FISH_AUDIO_API_KEY=your-fish-audio-api-key
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | Discord bot token; startup fails without it |
| `DEEPSEEK_API_KEY` | at least one of the two | DeepSeek TTS key; enables DeepSeek voices |
| `FISH_AUDIO_API_KEY` | at least one of the two | Fish Audio TTS key; enables Fish Audio voices |

If neither TTS key is set, startup fails with `no tts provider api key`.

### Voice configuration (`config/tts_config.json`)

The file is an array; each entry describes one available voice:

```json
[
  {
    "provider_id": "DEEPSEEK",
    "voice_name": "deepseek-stella",
    "api_parameters": {
      "audio_format_type": "pcm",
      "voice_id": "stella",
      "filter_markdown": true
    }
  },
  {
    "provider_id": "FISH_AUDIO",
    "voice_name": "fish-s2.1-pro",
    "api_parameters": {
      "model": "s2.1-pro-free",
      "format": "pcm"
    }
  }
]
```

| Field | Required | Description |
|---|---|---|
| `provider_id` | yes | `DEEPSEEK` or `FISH_AUDIO` |
| `voice_name` | yes | Name shown to users; must be unique and is what `/voice` uses |
| `api_parameters` | no | Provider-specific parameters, see below |

DeepSeek `api_parameters`:

| Parameter | Default | Description |
|---|---|---|
| `audio_format_type` | `pcm` | Output format, `pcm` or `opus`; playback supports `pcm` only |
| `voice_id` | `stella` | Provider voice ID, e.g. `stella` or `tide` |
| `filter_markdown` | `true` | Whether Markdown is stripped before reading |

Fish Audio `api_parameters`:

| Parameter | Default | Description |
|---|---|---|
| `model` | required | `s1`, `s2-pro`, `s2.1-pro`, `s2.1-pro-free`, `drama-3`, `s3-pro` |
| `reference_id` | `null` | Voice model ID; `null` uses the default voice |
| `format` | `pcm` | `wav`, `pcm`, `mp3`, `opus`; playback supports `pcm` only |
| `sample_rate` | format default | Output sample rate in Hz; must be positive |
| `mp3_bitrate` | format default | MP3 bitrate in kbps, only for `format=mp3` |
| `opus_bitrate` | format default | Opus bitrate in bps, only for `format=opus` |
| `temperature` | `0.7` | Sampling temperature, 0-1 |
| `top_p` | `0.7` | Nucleus sampling probability, 0-1 |
| `chunk_length` | `300` | Text segment size, 100-300 |
| `min_chunk_length` | `50` | Minimum characters before a new segment, 0-100 |
| `normalize` | `true` | Whether numbers, dates and symbols are spelled out |
| `language` | `null` | BCP-47 language tag (max 35 chars); `null` detects per chunk |
| `latency` | `normal` | `low`, `normal` or `balanced` |
| `max_new_tokens` | `1024` | Max audio tokens per text chunk; must be > 0 |
| `repetition_penalty` | `1.2` | Penalty for repeating audio patterns |
| `condition_on_previous_chunks` | `true` | Reuse earlier audio to keep the voice consistent |
| `early_stop_threshold` | `1` | Early stopping threshold, 0-1 |

`api_parameters` are passed through unchanged; see the official API docs for the meaning and valid range of each parameter: [DeepSeek Text to Speech](https://api-docs.deepseek.com/guides/tts/), [Fish Audio TTS Live](https://docs.fish.audio/api-reference/endpoint/websocket/tts-live).

Loading rules:

- Only voices whose provider has a configured API key are kept; if none remain, startup fails.
- Duplicate `voice_name` values make loading fail.
- The first remaining entry is the default voice.
- Run `/reload_voices` after editing the file; a failed reload keeps the previous configuration.

### Local audio library (`config/sound_pad/`)

Put audio files (e.g. `.mp3`, `.m4a`) into `config/sound_pad/`. The directory is scanned at startup and each file name without its extension becomes the sound name used by `/play` and `/songs`. Run `/reload_songs` after adding or removing files.

## Commands

Type `/` in the chat box to pick a command. Before using playback commands, join a voice channel and run `/connect`, otherwise the bot replies `The bot is not in a voice channel`.

### Voice channel

| Command | Parameter | Description |
|---|---|---|
| `/connect` | none | Join your voice channel; moves the bot if it is in another one |
| `/quit` | none | Leave the current voice channel |

### Playback control

| Command | Parameter | Description |
|---|---|---|
| `/pause` | none | Pause the current playback |
| `/resume` | none | Resume playback |
| `/next` | none | Skip the current audio and start the next queued one |
| `/volume_tts` | `volume` (optional integer 0-200, percent) | Without a value it shows the speech volume; with a value it sets it, e.g. `/volume_tts volume: 120` |
| `/volume_song` | `volume` (optional integer 0-200, percent) | Same for the local audio playback, e.g. `/volume_song volume: 80` |

### Speech

| Command | Parameter | Description |
|---|---|---|
| `/tts` | `text` (required) | Read the text with your selected voice, e.g. `/tts text: Hello everyone` |
| `/voice` | `voice_name` (required) | Switch the voice used for later requests; prefix autocomplete supported |
| `/voices` | none | List all voices and mark the one you use |
| `/reload_voices` | none | Reload `config/tts_config.json` so changes apply immediately |

### Local audio

| Command | Parameter | Description |
|---|---|---|
| `/play` | `sound_name` (required) | Play a sound from the library; prefix autocomplete supported, e.g. `/play sound_name: long-day` |
| `/songs` | none | List every sound name in the library |
| `/reload_songs` | none | Rescan `config/sound_pad/` so added or removed files apply immediately |

Notes:

- `/tts` audio has higher priority than `/play` and is queued ahead of pending items.
- A guild plays one item at a time and the pending queue holds up to 20 items; when it is full the bot replies `The audio queue is full.`
- Results of `/songs`, `/voices` and similar queries are visible only to the caller.
