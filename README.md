# Vendly Video Metadata Scrubber Bot

A Telegram bot that strips all metadata from video files with zero quality loss. No re-encoding. No data stored. Open source.

**Live bot: [@VendlyVideoBot](https://t.me/VendlyVideoBot)**  
**By [Vendly Privacy Labs](https://vendlyprivacylabs.com)**

---

## What it does

Every video you record embeds hidden metadata — GPS coordinates, device make and model, timestamps, encoder settings, and sometimes your name. This bot removes all of it in seconds.

- Strips GPS location, device ID, timestamps and all metadata streams
- Zero quality loss — video and audio streams are copied exactly, not re-encoded
- Supports MP4, MOV, MKV, AVI, WEBM (up to 50 MB)
- Runs entirely through Telegram — no account, no sign up

---

## Zero storage architecture

```
User sends video
       │
       ▼
 Download to secure
   temp file (/tmp)
       │
       ▼
  ffmpeg strips
  all metadata
  (no re-encode)
       │
       ▼
 Clean file sent
   back to user
       │
       ▼
 Both temp files
 deleted immediately
 (finally block —
  even on failure)
       │
       ▼
  Nothing retained.
  Ever.
```

The `finally` block in `handle_video()` guarantees both temp files are deleted even if an exception occurs mid-process. There are no logs of user files, no database writes, no persistent storage of any kind.

---

## How it works

ffmpeg is called with three key flags:

```bash
ffmpeg -i input.mp4 \
  -map_metadata -1 \   # remove all metadata streams
  -map 0 \             # include all video/audio/subtitle streams
  -c:v copy \          # copy video — no re-encode
  -c:a copy \          # copy audio — no re-encode
  -c:s copy \          # copy subtitles if present
  -movflags faststart \ # web-optimised atom ordering
  output.mp4
```

The output file is byte-for-byte identical in quality to the input. Only the metadata containers are stripped.

---

## Self-hosting

### Requirements

- Python 3.10+
- ffmpeg installed on the system (`apt install ffmpeg` / `brew install ffmpeg`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Install

```bash
git clone https://github.com/Vendlyprivacylabs/Vendly-Privacy-Labs-video-scrubber-bot
cd Vendly-Privacy-Labs-video-scrubber-bot
pip install -r requirements.txt
```

### Configure

```bash
export VIDEO_BOT_TOKEN="your_bot_token_here"
```

Or create a `.env` file:

```
VIDEO_BOT_TOKEN=your_bot_token_here
```

### Run

```bash
python bot.py
```

---

## Rate limiting

The bot includes a built-in escalating ban system to prevent abuse:

| Offence | Restriction |
|---------|-------------|
| 1st     | 1 hour      |
| 2nd     | 1 day       |
| 3rd     | 1 week      |
| 4th+    | Permanent   |

Users are limited to 10 videos per 60 seconds. Bans are stored in `video_bans.json` and survive restarts.

---

## Requirements

```
aiogram>=3.0
```

ffmpeg must be installed separately on the host system.

---

## License

MIT — free to use, modify and self-host.

---

*Part of the [Vendly Privacy Labs](https://vendlyprivacylabs.com) open source toolkit.*
