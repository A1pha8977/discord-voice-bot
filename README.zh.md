# my-tts-bot

[English](README.md)

my-tts-bot 是一个用于 Discord 语音频道的机器人，支持播放本地音频文件，以及通过 DeepSeek 和 Fish Audio 实现多音色文本转语音（TTS）。

## 部署方式

需要 Python 3.12+、FFmpeg，以及一个已以 `bot`、`applications.commands` 作用域邀请、并拥有连接、发言、发送消息权限的 Discord Bot Token。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

启动前需先创建 `.env` 与 `config/` 下的配置文件，见下文配置章节。

启动参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--log-level` | `INFO` | 日志级别，可选 `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `--debug-module NAME` | 无 | 把指定 logger 调整为 `DEBUG`，可重复传入，例如 `--debug-module services.tts.fish_audio_tts_client` |

日志同时输出到控制台和 `logs/bot.log`，按天轮转，保留全部历史文件。

## 配置

`.env` 与 `config/` 默认被 `.gitignore` 忽略，需要自行创建。

### 环境变量（`.env`）

```dotenv
DISCORD_BOT_TOKEN=your-discord-bot-token
DEEPSEEK_API_KEY=your-deepseek-api-key
FISH_AUDIO_API_KEY=your-fish-audio-api-key
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | 是 | Discord Bot Token，缺失时启动报错 |
| `DEEPSEEK_API_KEY` | 二者至少一个 | DeepSeek TTS 密钥，配置后启用 DeepSeek 语音 |
| `FISH_AUDIO_API_KEY` | 二者至少一个 | Fish Audio TTS 密钥，配置后启用 Fish Audio 语音 |

两个 TTS 密钥都没有时，启动会报 `no tts provider api key`。

### 语音配置（`config/tts_config.json`）

文件内容是一个数组，每个元素描述一个可用语音：

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

| 字段 | 必填 | 说明 |
|---|---|---|
| `provider_id` | 是 | `DEEPSEEK` 或 `FISH_AUDIO` |
| `voice_name` | 是 | 展示给用户的语音名称，必须唯一，`/voice` 使用该名称 |
| `api_parameters` | 否 | 传给对应提供商的参数，见下表 |

DeepSeek 的 `api_parameters`：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `audio_format_type` | `pcm` | 输出格式，`pcm` 或 `opus`；播放仅支持 `pcm` |
| `voice_id` | `stella` | 提供商语音 ID，例如 `stella`、`tide` |
| `filter_markdown` | `true` | 是否在朗读前过滤 Markdown |

Fish Audio 的 `api_parameters`：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `model` | 必填 | `s1`、`s2-pro`、`s2.1-pro`、`s2.1-pro-free`、`drama-3`、`s3-pro` |
| `reference_id` | `null` | 音色模型 ID，`null` 表示使用默认音色 |
| `format` | `pcm` | `wav`、`pcm`、`mp3`、`opus`；播放仅支持 `pcm` |
| `sample_rate` | 格式默认值 | 输出采样率（Hz），须为正数 |
| `mp3_bitrate` | 格式默认值 | MP3 比特率（kbps），仅 `format=mp3` 时生效 |
| `opus_bitrate` | 格式默认值 | Opus 比特率（bps），仅 `format=opus` 时生效 |
| `temperature` | `0.7` | 采样温度，范围 0-1 |
| `top_p` | `0.7` | 核采样概率，范围 0-1 |
| `chunk_length` | `300` | 文本分段长度，范围 100-300 |
| `min_chunk_length` | `50` | 新分段的最小字符数，范围 0-100 |
| `normalize` | `true` | 是否把数字、日期、符号展开为文字 |
| `language` | `null` | BCP-47 语言标签（最长 35 字符），`null` 表示按分段自动识别 |
| `latency` | `normal` | `low`、`normal`、`balanced` |
| `max_new_tokens` | `1024` | 每个文本分段生成的最大音频 token 数，须大于 0 |
| `repetition_penalty` | `1.2` | 重复音频的惩罚系数 |
| `condition_on_previous_chunks` | `true` | 是否用前文音频保持音色一致 |
| `early_stop_threshold` | `1` | 提前停止阈值，范围 0-1 |

`api_parameters` 只做透传，各参数的含义与取值范围以官方 API 文档为准：[DeepSeek 语音合成](https://api-docs.deepseek.com/zh-cn/guides/tts/)、[Fish Audio TTS Live](https://docs.fish.audio/api-reference/endpoint/websocket/tts-live)。

加载规则：

- 只保留当前已配置 API Key 的提供商对应的语音；过滤后没有任何语音时启动失败。
- `voice_name` 重复会导致加载失败。
- 过滤后的第一条语音是默认语音。
- 修改文件后执行 `/reload_voices` 热重载；加载失败时保留原有配置。

### 本地音频库（`config/sound_pad/`）

把音频文件（如 `.mp3`、`.m4a`）放入 `config/sound_pad/`，启动时自动扫描，文件名（不含扩展名）即为 `/play` 与 `/songs` 使用的音频名称。新增或删除文件后执行 `/reload_songs` 生效。

## 命令使用方法

在聊天框输入 `/` 选择命令。使用播放类命令前，先加入语音频道并执行 `/connect`，否则机器人会回复 `The bot is not in a voice channel`。

### 语音频道

| 命令 | 参数 | 说明 |
|---|---|---|
| `/connect` | 无 | 加入你所在的语音频道；已在其他频道时会移动过去 |
| `/quit` | 无 | 离开当前语音频道 |

### 播放控制

| 命令 | 参数 | 说明 |
|---|---|---|
| `/pause` | 无 | 暂停当前播放 |
| `/resume` | 无 | 继续播放 |
| `/next` | 无 | 跳过当前音频，播放队列中的下一条 |
| `/volume_tts` | `volume`（可选，0-200 的整数，单位为百分比） | 不带参数时显示语音播报音量；带参数时设置，例如 `/volume_tts volume: 120` |
| `/volume_song` | `volume`（可选，0-200 的整数，单位为百分比） | 不带参数时显示本地音频音量；带参数时设置，例如 `/volume_song volume: 80` |

### 语音播报

| 命令 | 参数 | 说明 |
|---|---|---|
| `/tts` | `text`（必填） | 用你选择的语音朗读文本，例如 `/tts text: 大家好，欢迎来到频道` |
| `/voice` | `voice_name`（必填） | 切换你之后使用的语音，支持前缀自动补全 |
| `/voices` | 无 | 列出全部语音，并标出你当前使用的语音 |
| `/reload_voices` | 无 | 重新加载 `config/tts_config.json`，使改动立即生效 |

### 本地音频

| 命令 | 参数 | 说明 |
|---|---|---|
| `/play` | `sound_name`（必填） | 播放音频库中的音频，支持前缀自动补全，例如 `/play sound_name: long-day` |
| `/songs` | 无 | 列出音频库中的全部音频名称 |
| `/reload_songs` | 无 | 重新扫描 `config/sound_pad/`，使新增或删除的音频立即生效 |

补充说明：

- `/tts` 生成的音频优先级高于 `/play`，会插到待播队列前面。
- 每个服务器同时只播放一条音频，待播队列上限为 20 条；队列已满时回复 `The audio queue is full.`。
- `/songs`、`/voices` 等查询结果仅对执行者可见。
