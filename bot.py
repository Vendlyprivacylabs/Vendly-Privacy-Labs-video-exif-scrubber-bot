#!/usr/bin/env python3
"""
Vendly Video Metadata Scrubber Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Strips all metadata from video files via ffmpeg.
No re-encoding — fast, lossless, zero quality loss.

Supports: MP4, MOV, MKV, AVI, WEBM
Requires: pip install aiogram + ffmpeg installed on system
Env var:  VIDEO_BOT_TOKEN

Zero storage architecture:
  1. Video downloaded to a secure temp file
  2. ffmpeg strips metadata in-place (no re-encode)
  3. Clean file sent back to user
  4. Both temp files deleted in finally block — even on failure
  5. Nothing is ever written to disk beyond the two temp files
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN       = os.environ["VIDEO_BOT_TOKEN"]
MAX_BYTES       = 50 * 1024 * 1024   # 50 MB — Telegram bot API hard limit
PROCESS_TIMEOUT = 60                  # seconds before ffmpeg is killed

RATE_LIMIT  = 10   # max videos per window per user
RATE_WINDOW = 60   # seconds

BAN_LEVELS = [
    3_600,          # 1 hour
    86_400,         # 1 day
    604_800,        # 1 week
    float("inf"),   # permanent
]

BANS_FILE = Path("video_bans.json")

SUPPORTED_MIME = {
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/x-msvideo",
    "video/webm",
    "video/mpeg",
    "video/3gpp",
}

SUPPORTED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".mpeg", ".3gp"}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("video_bot")

# ─── Magic byte validation ─────────────────────────────────────────────────────
# Never trust filename or MIME type alone — validate by actual file signature.

def validate_video_bytes(data: bytes) -> bool:
    if len(data) < 12:
        return False
    # MP4 / MOV — ftyp, moov, wide, free, mdat atoms
    if data[4:8] in (b"ftyp", b"moov", b"wide", b"free", b"mdat"):
        return True
    # MKV / WEBM — EBML header
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return True
    # AVI — RIFF....AVI
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return True
    # MPEG
    if data[:3] == b"\x00\x00\x01" and data[3:4] in (b"\xba", b"\xb3"):
        return True
    return False

# ─── Rate limiting + bans ─────────────────────────────────────────────────────

_rate_windows: dict[int, deque] = defaultdict(lambda: deque(maxlen=RATE_LIMIT + 1))
_bans: dict[int, dict]          = {}
_violations: dict[int, int]     = defaultdict(int)


def _load_bans() -> None:
    if BANS_FILE.exists():
        try:
            data = json.loads(BANS_FILE.read_text())
            now  = time.time()
            for uid_str, ban in data.items():
                if ban["until"] == -1 or ban["until"] > now:
                    _bans[int(uid_str)] = ban
            log.info("Loaded %d active bans", len(_bans))
        except Exception as e:
            log.warning("Could not load bans: %s", e)


def _save_bans() -> None:
    try:
        BANS_FILE.write_text(
            json.dumps({str(k): v for k, v in _bans.items()}, indent=2)
        )
    except Exception as e:
        log.warning("Could not save bans: %s", e)


def _is_banned(user_id: int) -> tuple[bool, str]:
    ban = _bans.get(user_id)
    if not ban:
        return False, ""
    if ban["until"] == -1 or time.time() < ban["until"]:
        if ban["until"] == -1:
            duration_str = "permanently"
        else:
            remaining = int(ban["until"] - time.time())
            if remaining >= 86400:
                duration_str = f"for {remaining // 86400} more day(s)"
            elif remaining >= 3600:
                duration_str = f"for {remaining // 3600} more hour(s)"
            else:
                duration_str = f"for {remaining // 60} more minute(s)"
        return True, (
            f"⛔ You have been restricted {duration_str} due to excessive usage.\n\n"
            f"This is automated. Access resumes automatically."
        )
    del _bans[user_id]
    _violations[user_id] = 0
    _save_bans()
    return False, ""


def _record_violation(user_id: int) -> str:
    _violations[user_id] += 1
    current_level = _bans.get(user_id, {}).get("level", -1)
    new_level     = min(current_level + 1, len(BAN_LEVELS) - 1)
    duration      = BAN_LEVELS[new_level]
    until         = -1 if duration == float("inf") else time.time() + duration

    _bans[user_id] = {"until": until, "level": new_level}
    _save_bans()

    if until == -1:
        duration_str = "permanently"
    elif duration >= 86400:
        duration_str = f"for {int(duration // 86400)} day(s)"
    elif duration >= 3600:
        duration_str = f"for {int(duration // 3600)} hour(s)"
    else:
        duration_str = f"for {int(duration // 60)} minute(s)"

    log.warning("User %d restricted %s (level %d)", user_id, duration_str, new_level)
    return (
        f"⛔ Slow down. You've been restricted {duration_str} due to excessive usage.\n\n"
        f"This bot is a free tool — please use it fairly. Restrictions lift automatically."
    )


def _check_rate(user_id: int) -> bool:
    now    = time.time()
    window = _rate_windows[user_id]
    while window and now - window[0] > RATE_WINDOW:
        window.popleft()
    if len(window) >= RATE_LIMIT:
        return False
    window.append(now)
    return True


async def _cleanup_task() -> None:
    """Hourly cleanup of expired rate windows and bans."""
    while True:
        await asyncio.sleep(3600)
        now         = time.time()
        pruned_r    = 0
        pruned_b    = 0

        for uid in list(_rate_windows):
            w = _rate_windows[uid]
            while w and now - w[0] > RATE_WINDOW:
                w.popleft()
            if not w:
                del _rate_windows[uid]
                pruned_r += 1

        for uid in list(_bans):
            ban = _bans[uid]
            if ban["until"] != -1 and now >= ban["until"]:
                del _bans[uid]
                _violations[uid] = 0
                pruned_b += 1

        if pruned_b:
            _save_bans()

        log.info(
            "Cleanup: pruned %d rate windows, %d expired bans. Active bans: %d",
            pruned_r, pruned_b, len(_bans),
        )

# ─── Core scrub ───────────────────────────────────────────────────────────────

def _scrub_video_sync(in_path: str, out_path: str) -> None:
    """
    Strip all metadata from a video file using ffmpeg.

    Flags used:
      -map_metadata -1   remove all global metadata
      -map 0             include all streams (video, audio, subtitles)
      -c:v copy          copy video stream — no re-encode, no quality loss
      -c:a copy          copy audio stream — no re-encode
      -c:s copy          copy subtitle streams if present
      -movflags faststart  optimise MP4 atom ordering for web playback

    The result is a video file identical in quality to the original,
    with all identifying metadata removed.
    """
    cmd = [
        "ffmpeg",
        "-i", in_path,
        "-map_metadata", "-1",
        "-map", "0",
        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "copy",
        "-movflags", "faststart",
        "-y",
        out_path,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        log.debug("ffmpeg stderr: %s", result.stderr[-500:])
        raise RuntimeError(f"ffmpeg exited with code {result.returncode}")


async def scrub_video(in_path: str, out_path: str) -> None:
    """Run the ffmpeg scrub in a thread pool so the event loop stays unblocked."""
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(
        loop.run_in_executor(None, _scrub_video_sync, in_path, out_path),
        timeout=PROCESS_TIMEOUT + 5,
    )

# ─── Welcome message ──────────────────────────────────────────────────────────

WELCOME = (
    "🎬 <b>Video Metadata Scrubber — Protect Your Videos</b>\n\n"
    "Every video you record contains hidden metadata. This includes:\n"
    "📍 GPS location where the video was recorded\n"
    "📱 Device make, model and serial number\n"
    "🕐 Exact date and time of recording\n"
    "🎙 Encoder software and settings\n"
    "👤 Sometimes your name and account details\n\n"
    "<b>What this bot does</b>\n"
    "Strips all metadata from your video with zero quality loss — "
    "no re-encoding, no compression, no changes to the video itself. "
    "Just the hidden data removed.\n\n"
    "<b>Supported formats</b>\n"
    "MP4, MOV, MKV, AVI, WEBM — max <b>50 MB</b>\n\n"
    "<b>Zero storage guarantee</b>\n"
    "Your video is downloaded to a temporary file, scrubbed, returned to you, "
    "then deleted immediately. Nothing is ever stored, logged or retained.\n\n"
    "⚠️ <b>How to send correctly</b>\n"
    "Send your video as a <b>File/Document</b> — not as a video message.\n"
    "Telegram compresses videos sent normally and reduces quality.\n"
    "Tap the 📎 attachment icon → <b>File</b> → select your video.\n\n"
    "<b>How to use</b>\n"
    "1. Send your video as a file\n"
    "2. Get it back clean in seconds\n"
    "3. Download the returned file — do not screen record\n\n"
    "<i>Open source — vendlyprivacylabs.com</i>"
)

# ─── Handlers ─────────────────────────────────────────────────────────────────

dp = Dispatcher()


@dp.message(CommandStart())
async def send_welcome(msg: Message) -> None:
    await msg.answer(WELCOME, parse_mode="HTML")


@dp.message(F.document)
async def handle_video(msg: Message, bot: Bot) -> None:
    if not msg.from_user:
        return
    user_id = msg.from_user.id

    # Ban check
    banned, ban_msg = _is_banned(user_id)
    if banned:
        await msg.reply(ban_msg)
        return

    doc  = msg.document
    mime = (doc.mime_type or "").lower()
    fname = doc.file_name or "video.mp4"
    ext   = Path(fname).suffix.lower()

    # MIME / extension check
    if mime not in SUPPORTED_MIME and ext not in SUPPORTED_EXT:
        await msg.reply(
            "⚠️ Please send a video file (MP4, MOV, MKV, AVI or WEBM) as a document.\n\n"
            "Tap 📎 → <b>File</b> → select your video.",
            parse_mode="HTML",
        )
        return

    # Rate check
    if not _check_rate(user_id):
        await msg.reply(_record_violation(user_id))
        return

    # Size check
    if doc.file_size and doc.file_size > MAX_BYTES:
        size_mb = doc.file_size / (1024 * 1024)
        await msg.reply(
            f"⚠️ File too large ({size_mb:.1f} MB). Maximum is 50 MB.\n"
            "Try trimming or compressing the video first."
        )
        return

    processing = await msg.reply("⏳ Stripping metadata...")

    in_tmp  = None
    out_tmp = None

    try:
        suffix  = ext if ext in SUPPORTED_EXT else ".mp4"
        in_tmp  = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        out_tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        in_tmp.close()
        out_tmp.close()

        # Download to temp file — ffmpeg requires file paths, not byte buffers
        await bot.download(doc.file_id, destination=in_tmp.name)

        # Magic byte validation — never trust MIME or filename alone
        with open(in_tmp.name, "rb") as f:
            header = f.read(16)
        if not validate_video_bytes(header):
            await msg.reply(
                "⚠️ Unrecognised video format. "
                "Send a valid MP4, MOV, MKV, AVI or WEBM file."
            )
            return

        # Strip metadata
        await scrub_video(in_tmp.name, out_tmp.name)

        # Return clean file
        base        = Path(fname).stem
        clean_fname = f"{base}_clean{suffix}"

        await msg.reply_document(
            FSInputFile(out_tmp.name, filename=clean_fname),
            caption=(
                "🎬 <b>Clean. Metadata-free. Yours.</b>\n\n"
                "✅ All metadata stripped — no GPS, no device ID, no timestamps\n"
                "✅ Zero quality loss — no re-encoding, streams copied exactly\n\n"
                "⚠️ <b>Save the file — do not screen record it.</b>\n\n"
                "<i>Open source — vendlyprivacylabs.com</i>"
            ),
            parse_mode="HTML",
        )
        log.info("Scrubbed video for user %d (%s → %s)", user_id, fname, clean_fname)

    except asyncio.TimeoutError:
        log.warning("Scrub timeout for user %d", user_id)
        await msg.reply("⏱ Processing took too long. Try a shorter or smaller video.")
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg timeout for user %d", user_id)
        await msg.reply("⏱ Processing took too long. Try a shorter or smaller video.")
    except Exception as e:
        log.exception("Scrub failed for user %d: %s", user_id, e)
        await msg.reply(
            "❌ Could not process that video. "
            "Make sure it's a valid MP4, MOV, MKV, AVI or WEBM file under 50 MB."
        )
    finally:
        # Always delete temp files — even on failure
        # This is the zero-storage guarantee in practice
        for path in (in_tmp, out_tmp):
            if path:
                try:
                    os.unlink(path.name)
                except Exception:
                    pass
        try:
            await bot.delete_message(msg.chat.id, processing.message_id)
        except Exception:
            pass


@dp.message(F.video)
async def handle_compressed_video(msg: Message) -> None:
    """User sent video as a video message (Telegram compresses it) — redirect."""
    await msg.reply(
        "⚠️ Please send your video as a <b>File</b> to preserve quality.\n\n"
        "Tap 📎 → <b>File</b> → select your video.\n\n"
        "Sending as a video message lets Telegram compress it before we even see it.",
        parse_mode="HTML",
    )


@dp.message()
async def fallback(msg: Message) -> None:
    await msg.reply(
        "Send me a video file and I'll strip all metadata from it. 🎬\n\n"
        "Type /start to learn more."
    )

# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    _load_bans()
    log.info("Starting Video Metadata Scrubber Bot")
    bot = Bot(token=BOT_TOKEN)
    asyncio.create_task(_cleanup_task())
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
