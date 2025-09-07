
import os
import logging
import requests
from uuid import uuid4
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler
)
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import asyncio
import time
import subprocess
from datetime import datetime, timedelta
import json
from collections import defaultdict
import glob
import sys
import shutil
import openai

# The GIF URLs for each command
START_GIF = "https://media0.giphy.com/media/v1.Y2lkPTc5MGI3NjExMXU4a3oyend6b2trZnlmampmajNkb3l0cGFsNGZoNTl6NmY0cGJnZiZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/ORjfgiG9ZtxcQQwZzv/giphy.gif"
MENU_GIF = "https://i.ibb.co/CKQQg4f5/shaban-md.jpg"

# Track first-time users and store user data
user_data = defaultdict(dict)
user_requests = defaultdict(list)
RATE_LIMIT_INTERVAL = 60
RATE_LIMIT_COUNT = 5

ADMINS = [7819091632]
banned_users = set()
bot_start_time = time.time()
maintenance_mode = False

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====== CONFIG (yours kept intact) ======
TOKEN = "7756702380:AAE8u2r-bmx21MZY2ROs6JbQIE-OrOMvXjU"
SPOTIFY_CLIENT_ID = "539a3af17aa24fbab30bd16b9a6551cd"
SPOTIFY_CLIENT_SECRET = "c5c1d9354966474eb4a705bf3e2c8880"
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"
openai.api_key = OPENAI_API_KEY

# Spotify client
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

# ============== OpenAI Command ==============
async def ask_command(update: Update, context: CallbackContext) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /ask <your question>")
        return
    query = " ".join(context.args)
    await update.message.reply_text("🤖 Thinking...")
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": query}],
            max_tokens=200
        )
        answer = response.choices[0].message.content.strip()
        await update.message.reply_text(f"💡 {answer}")
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        await update.message.reply_text("❌ Sorry, I couldn't process your request.")

# ============== YouTube Handler ==============
async def handle_url(update: Update, context: CallbackContext, url: str) -> None:
    if "youtube.com" in url or "youtu.be" in url:
        keyboard = [[
            InlineKeyboardButton("🎵 Download MP3", callback_data=f"download_option:audio:{url}"),
            InlineKeyboardButton("🎥 Download MP4", callback_data=f"download_option:video:{url}"),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("YouTube URL detected. Choose format:", reply_markup=reply_markup)
        return
    await update.message.reply_text("Unsupported URL for now.")

# ============== Download Processor ==============
async def process_download(update: Update, context: CallbackContext, format_type: str, url: str):
    ydl_opts = {
        'outtmpl': '%(title)s.%(ext)s',
        'cookiefile': 'cookies.txt'
    }

    if format_type == "audio":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    elif format_type == "video":
        ydl_opts.update({'format': 'best'})  

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_name = ydl.prepare_filename(info)
            if format_type == "audio":
                file_name = file_name.rsplit(".", 1)[0] + ".mp3"

        await context.bot.send_document(chat_id=update.effective_chat.id, document=open(file_name, "rb"))
        os.remove(file_name)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# ============== Callback Handler ==============
async def handle_callback_query(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("download_option:"):
        _, format_type, url = query.data.split(":", 2)
        await process_download(update, context, format_type, url)

# ============== Dummy Other Commands ==============
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("👋 Welcome!")

async def menu_command(update: Update, context: CallbackContext):
    await update.message.reply_text("📜 Menu here.")

async def help_command(update: Update, context: CallbackContext):
    await update.message.reply_text("ℹ️ Help info.")

async def artist_command(update: Update, context: CallbackContext):
    await update.message.reply_text("🎤 Artist info.")

async def about_command(update: Update, context: CallbackContext):
    await update.message.reply_text("🤖 About this bot.")

async def stats_command(update: Update, context: CallbackContext):
    uptime = str(timedelta(seconds=int(time.time() - bot_start_time)))
    await update.message.reply_text(f"📊 Uptime: {uptime}")

async def handle_message(update: Update, context: CallbackContext):
    text = update.message.text
    if "youtube.com" in text or "youtu.be" in text:
        await handle_url(update, context, text)
    else:
        await update.message.reply_text("🔍 Message received.")

# ============== MAIN ==============
def main() -> None:
    global application
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("artist", artist_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("ask", ask_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    application.run_polling(poll_interval=0.1)

if __name__ == "__main__":
    main()
