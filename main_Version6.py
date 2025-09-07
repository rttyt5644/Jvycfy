#!/usr/bin/env python3
"""
YouTube → MP3/MP4 Telegram bot (fixed robustness, cookies.txt support)

Notes:
- Uses the token and admin id you provided.
- Requires ffmpeg in PATH for audio extraction/merging.
- Places downloads in a temporary directory; files uploaded from open file handles.
- Cookies: if cookies.txt exists in working dir it will be used; you can upload via /uploadcookies.
"""

import os
import re
import logging
import asyncio
import tempfile
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

from yt_dlp import YoutubeDL
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG (user-provided) ----------------
TELEGRAM_TOKEN = "7756702380:AAE8u2r-bmx21MZY2ROs6JbQIE-OrOMvXjU"
ADMINS = [7819091632]  # admin chat id(s) from your message
COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")
MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", "45"))
# --------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

YT_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/\S+", re.IGNORECASE)


# ---------------- Utilities ----------------
class YTDLLogger:
    def __init__(self):
        self.last_error = None
        self._log = logging.getLogger("yt_dlp")

    def debug(self, msg):
        self._log.debug(msg)

    def info(self, msg):
        self._log.info(msg)

    def warning(self, msg):
        self._log.warning(msg)

    def error(self, msg):
        try:
            s = msg.decode() if isinstance(msg, (bytes, bytearray)) else str(msg)
        except Exception:
            s = str(msg)
        self.last_error = s
        self._log.error(s)


def check_ffmpeg() -> bool:
    ok = shutil.which("ffmpeg") is not None
    if not ok:
        logger.warning("ffmpeg not found in PATH. Install ffmpeg for audio/video postprocessing.")
    else:
        logger.info("ffmpeg OK: %s", shutil.which("ffmpeg"))
    return ok


def make_progress_hook(loop: asyncio.AbstractEventLoop, app, chat_id: int, message_id: int):
    """
    Returns a hook to pass to yt-dlp. This runs in the download thread and schedules edits in the event loop.
    """
    last = {"percent": None, "time": 0}

    def hook(d):
        status = d.get("status")
        now = time.time()
        if status == "downloading":
            percent = d.get("_percent_str") or d.get("percent") or ""
            eta = d.get("_eta_str") or ""
            # throttle edits
            if last["percent"] != percent and (now - last["time"] > 1.5):
                last["percent"] = percent
                last["time"] = now
                text = f"Downloading... {percent} ETA {eta}"
                coro = app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                loop.call_soon_threadsafe(asyncio.create_task, coro)
        elif status == "finished":
            coro = app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Download finished, processing...")
            loop.call_soon_threadsafe(asyncio.create_task, coro)

    return hook


# ---------------- Blocking helpers (run in thread) ----------------
def yt_opts_audio(outtmpl: str, cookiefile: Optional[str], progress_hook, ytdl_logger: YTDLLogger):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "merge_output_format": "mp3",
        "quiet": True,
        "no_warnings": True,
        "logger": ytdl_logger,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def yt_opts_video(outtmpl: str, format_spec: str, cookiefile: Optional[str], progress_hook, ytdl_logger: YTDLLogger):
    opts = {
        "format": format_spec,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "logger": ytdl_logger,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def run_yt_dlp_audio(url: str, outtmpl: str, cookiefile: Optional[str], progress_hook, ytdl_logger: YTDLLogger) -> Tuple[str, dict]:
    """
    Blocking: downloads audio and returns final mp3 path and info dict.
    """
    opts = yt_opts_audio(outtmpl, cookiefile, progress_hook, ytdl_logger)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = ydl.prepare_filename(info)
        mp3 = str(Path(prepared).with_suffix(".mp3"))
        if Path(mp3).exists():
            return mp3, info
        # fallback search
        parent = Path(prepared).parent
        candidates = list(parent.glob("*.mp3"))
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
            return str(candidates[0]), info
        raise FileNotFoundError("MP3 not found after yt-dlp processing.")


def run_yt_dlp_video(url: str, outtmpl: str, format_spec: str, cookiefile: Optional[str], progress_hook, ytdl_logger: YTDLLogger) -> Tuple[str, dict]:
    opts = yt_opts_video(outtmpl, format_spec, cookiefile, progress_hook, ytdl_logger)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = ydl.prepare_filename(info)
        mp4 = str(Path(prepared).with_suffix(".mp4"))
        if Path(mp4).exists():
            return mp4, info
        if Path(prepared).exists():
            return prepared, info
        parent = Path(prepared).parent
        candidates = []
        for ext in ("*.mp4", "*.mkv", "*.webm", "*.mov"):
            candidates.extend(parent.glob(ext))
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
            return str(candidates[0]), info
        raise FileNotFoundError("Video file not found after yt-dlp processing.")


# ---------------- Bot handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎧 Download MP3", callback_data="menu:audio")],
        [InlineKeyboardButton("🎬 Download Video", callback_data="menu:video")],
        [InlineKeyboardButton("📁 Upload cookies.txt", callback_data="menu:upload_cookies")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    await update.message.reply_text("Welcome — choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "menu:audio":
        context.user_data["awaiting_audio_url"] = True
        await query.edit_message_text("Send a YouTube link for audio (youtube.com or youtu.be).")
    elif data == "menu:video":
        context.user_data["awaiting_video_url"] = True
        await query.edit_message_text("Send a YouTube link for video (youtube.com or youtu.be).")
    elif data == "menu:upload_cookies":
        context.user_data["awaiting_cookies_upload"] = True
        await query.edit_message_text("Upload your cookies.txt as a document (Netscape format).")
    elif data == "menu:help":
        await query.edit_message_text("Usage: Send a YouTube link after choosing audio or video. Use /uploadcookies to upload cookies.txt.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    # audio flow
    if context.user_data.get("awaiting_audio_url"):
        m = YT_URL_RE.search(text)
        if not m:
            await update.message.reply_text("Please send a valid YouTube link.")
            return
        url = m.group(0)
        context.user_data.pop("awaiting_audio_url", None)
        context.user_data["pending_audio_url"] = url
        kb = [
            [InlineKeyboardButton("128 kbps", callback_data="audio_q:128"),
             InlineKeyboardButton("192 kbps", callback_data="audio_q:192")],
            [InlineKeyboardButton("320 kbps", callback_data="audio_q:320"),
             InlineKeyboardButton("Cancel", callback_data="audio_q:cancel")],
        ]
        await update.message.reply_text(f"Audio URL received:\n{url}\nChoose bitrate:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # video flow
    if context.user_data.get("awaiting_video_url"):
        m = YT_URL_RE.search(text)
        if not m:
            await update.message.reply_text("Please send a valid YouTube link.")
            return
        url = m.group(0)
        context.user_data.pop("awaiting_video_url", None)
        context.user_data["pending_video_url"] = url
        kb = [
            [InlineKeyboardButton("360p", callback_data="video_q:360"),
             InlineKeyboardButton("720p", callback_data="video_q:720")],
            [InlineKeyboardButton("1080p", callback_data="video_q:1080"),
             InlineKeyboardButton("Best", callback_data="video_q:best")],
            [InlineKeyboardButton("Cancel", callback_data="video_q:cancel")],
        ]
        await update.message.reply_text(f"Video URL received:\n{url}\nChoose resolution:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # otherwise, show helpful hint
    await update.message.reply_text("Send /start to open the menu, or paste a YouTube link after choosing Download MP3/Video.")


async def audio_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "audio_q:cancel":
        context.user_data.pop("pending_audio_url", None)
        await query.edit_message_text("Cancelled.")
        return
    bitrate = int(data.split(":")[1])
    url = context.user_data.get("pending_audio_url")
    if not url:
        await query.edit_message_text("No URL found. Start again with /start.")
        return

    progress_msg = await query.edit_message_text("Preparing audio download...")
    loop = asyncio.get_running_loop()
    app = context.application
    cookiefile = COOKIES_FILE if Path(COOKIES_FILE).exists() else None
    ytdl_logger = YTDLLogger()
    progress_hook = make_progress_hook(loop, app, update.effective_chat.id, progress_msg.message_id)

    with tempfile.TemporaryDirectory(prefix="yt_audio_") as tmpdir:
        outtmpl = str(Path(tmpdir) / "%(title)s-%(id)s.%(ext)s")
        try:
            mp3_path, info = await asyncio.to_thread(run_yt_dlp_audio, url, outtmpl, cookiefile, progress_hook, ytdl_logger)
        except Exception as e:
            logger.exception("Audio download error")
            reply = f"Audio download failed: {e}"
            if ytdl_logger.last_error:
                reply += f"\n\nyt-dlp: {ytdl_logger.last_error}"
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id, text=reply)
            return

        if not Path(mp3_path).exists():
            listing = "\n".join(p.name for p in Path(tmpdir).iterdir())
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id,
                                            text=f"Download finished but file not found. Temp dir: {listing}")
            return

        size_mb = Path(mp3_path).stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILESIZE_MB:
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id,
                                            text=f"MP3 is {size_mb:.1f} MB (> {MAX_FILESIZE_MB} MB). File saved at: {mp3_path}")
            return

        try:
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id, text="Uploading MP3...")
            with open(mp3_path, "rb") as fh:
                await app.bot.send_document(chat_id=update.effective_chat.id, document=InputFile(fh, filename=Path(mp3_path).name),
                                            caption=f"{info.get('title','Audio')} — {bitrate} kbps")
            try:
                await app.bot.delete_message(chat_id=update.effective_chat.id, message_id=progress_msg.message_id)
            except Exception:
                pass
        except Exception as e:
            logger.exception("Upload failed")
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id, text=f"Upload error: {e}")
        finally:
            context.user_data.pop("pending_audio_url", None)


async def video_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "video_q:cancel":
        context.user_data.pop("pending_video_url", None)
        await query.edit_message_text("Cancelled.")
        return
    choice = data.split(":")[1]
    url = context.user_data.get("pending_video_url")
    if not url:
        await query.edit_message_text("No URL found. Start again with /start.")
        return

    # Map to yt-dlp format
    if choice == "360":
        format_spec = "bestvideo[height<=360]+bestaudio/best"
    elif choice == "720":
        format_spec = "bestvideo[height<=720]+bestaudio/best"
    elif choice == "1080":
        format_spec = "bestvideo[height<=1080]+bestaudio/best"
    else:
        format_spec = "bestvideo+bestaudio/best"

    progress_msg = await query.edit_message_text("Preparing video download...")
    loop = asyncio.get_running_loop()
    app = context.application
    cookiefile = COOKIES_FILE if Path(COOKIES_FILE).exists() else None
    ytdl_logger = YTDLLogger()
    progress_hook = make_progress_hook(loop, app, update.effective_chat.id, progress_msg.message_id)

    with tempfile.TemporaryDirectory(prefix="yt_video_") as tmpdir:
        outtmpl = str(Path(tmpdir) / "%(title)s-%(id)s.%(ext)s")
        try:
            video_path, info = await asyncio.to_thread(run_yt_dlp_video, url, outtmpl, format_spec, cookiefile, progress_hook, ytdl_logger)
        except Exception as e:
            logger.exception("Video download error")
            reply = f"Video download failed: {e}"
            if ytdl_logger.last_error:
                reply += f"\n\nyt-dlp: {ytdl_logger.last_error}"
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id, text=reply)
            return

        if not Path(video_path).exists():
            listing = "\n".join(p.name for p in Path(tmpdir).iterdir())
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id,
                                            text=f"Download finished but file not found. Temp dir: {listing}")
            return

        size_mb = Path(video_path).stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILESIZE_MB:
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id,
                                            text=f"Video is {size_mb:.1f} MB (> {MAX_FILESIZE_MB} MB). File saved at: {video_path}")
            return

        try:
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id, text="Uploading video...")
            with open(video_path, "rb") as fh:
                await app.bot.send_document(chat_id=update.effective_chat.id, document=InputFile(fh, filename=Path(video_path).name),
                                            caption=f"{info.get('title','Video')} — {choice}")
            try:
                await app.bot.delete_message(chat_id=update.effective_chat.id, message_id=progress_msg.message_id)
            except Exception:
                pass
        except Exception as e:
            logger.exception("Upload failed")
            await app.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=progress_msg.message_id, text=f"Upload error: {e}")
        finally:
            context.user_data.pop("pending_video_url", None)


async def upload_cookies_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return
    if not context.user_data.get("awaiting_cookies_upload"):
        await update.message.reply_text("Use /uploadcookies first, then send the cookies.txt file.")
        return
    doc = update.message.document
    save_path = Path(COOKIES_FILE)
    try:
        file = await doc.get_file()
        await file.download_to_drive(custom_path=str(save_path))
        await update.message.reply_text(f"Saved cookies to {save_path}.")
    except Exception as e:
        logger.exception("Failed to save cookies")
        await update.message.reply_text(f"Failed to save cookies: {e}")
    finally:
        context.user_data["awaiting_cookies_upload"] = False


async def uploadcookies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_cookies_upload"] = True
    await update.message.reply_text("Please upload cookies.txt now as a document (Netscape format).")


async def handle_callback_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes callback handlers for audio/video quality selections and menu."""
    data = update.callback_query.data
    if data.startswith("audio_q:"):
        await audio_quality_callback(update, context)
    elif data.startswith("video_q:"):
        await video_quality_callback(update, context)
    elif data.startswith("menu:"):
        await menu_callback(update, context)
    else:
        await update.callback_query.answer("Unknown action", show_alert=True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send /start to open menu. Use /uploadcookies to upload cookies.txt. Only download content you have rights to.")


# ---------------- Entrypoint ----------------
def main():
    check_ffmpeg()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("uploadcookies", uploadcookies_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_dispatcher))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, upload_cookies_handler))

    logger.info("Bot starting...")
    app.run_polling(poll_interval=1.0)


if __name__ == "__main__":
    main()