import os
import logging
import asyncio
import hashlib
import hmac
import json
import threading
import time
from urllib.parse import parse_qs

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import db

load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН")
WEBAPP_URL  = os.getenv("WEBAPP_URL", "https://ТВОЙ_USERNAME.github.io/music-player/")
SERVER_URL  = os.getenv("SERVER_URL", "https://твой-сервер.onrender.com")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════
#  Flask — API для Mini App
# ══════════════════════════════════════════
flask_app = Flask(__name__)
CORS(flask_app)


def validate_init_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram initData, возвращает user или None"""
    try:
        parsed = parse_qs(init_data)
        check_hash = parsed.get("hash", [None])[0]
        if not check_hash:
            return None

        # Собираем строку для проверки
        data_check = []
        for key in sorted(parsed.keys()):
            if key == "hash":
                continue
            data_check.append(f"{key}={parsed[key][0]}")
        data_check_string = "\n".join(data_check)

        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed   = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if computed != check_hash:
            return None

        user_json = parsed.get("user", [None])[0]
        if user_json:
            return json.loads(user_json)
        return None
    except Exception as e:
        logger.warning(f"validate_init_data error: {e}")
        return None


def get_user_id_from_request() -> int | None:
    """Извлечь user_id из запроса"""
    init_data = request.args.get("init_data") or request.form.get("init_data")
    if init_data:
        user = validate_init_data(init_data)
        if user:
            return user.get("id")
    # Для тестов — прямой user_id
    uid = request.args.get("user_id")
    if uid:
        return int(uid)
    return None


# ── Ping (чтобы не засыпал) ──

@flask_app.route("/ping")
def ping():
    return "pong"


# ── Плейлисты ──

@flask_app.route("/api/playlists")
def api_playlists():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    playlists = db.get_playlists(user_id)
    # Конвертируем в формат для фронтенда
    result = {}
    for name, data in playlists.items():
        result[name] = {
            "cover_url": f"{SERVER_URL}/api/file/{data['cover_file_id']}" if data.get("cover_file_id") else None,
            "tracks": [],
        }
        for t in data.get("tracks", []):
            result[name]["tracks"].append({
                "title":     t.get("title", "Неизвестный трек"),
                "artist":    t.get("artist", ""),
                "album":     t.get("album", ""),
                "duration":  t.get("duration", 0),
                "audio_url": f"{SERVER_URL}/api/file/{t['file_id']}",
                "thumb_url": f"{SERVER_URL}/api/file/{t['thumb_id']}" if t.get("thumb_id") else None,
                "file_id":   t.get("file_id", ""),
            })
    return jsonify(result)


@flask_app.route("/api/playlist", methods=["POST"])
def api_create_playlist():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401

    body = request.json or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    ok = db.create_playlist(user_id, name)
    if not ok:
        return jsonify({"error": "already exists"}), 409
    return jsonify({"ok": True})


@flask_app.route("/api/playlist/<name>", methods=["DELETE"])
def api_delete_playlist(name):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    db.delete_playlist(user_id, name)
    return jsonify({"ok": True})


@flask_app.route("/api/playlist/<name>/rename", methods=["POST"])
def api_rename_playlist(name):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    body = request.json or {}
    new_name = body.get("new_name", "").strip()
    if not new_name:
        return jsonify({"error": "new_name required"}), 400
    ok = db.rename_playlist(user_id, name, new_name)
    return jsonify({"ok": ok})


@flask_app.route("/api/playlist/<name>/track", methods=["DELETE"])
def api_remove_track(name):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    body = request.json or {}
    index = body.get("index", -1)
    ok = db.remove_track(user_id, name, index)
    return jsonify({"ok": ok})


# ── Буферные треки (от бота) ──

@flask_app.route("/api/pending")
def api_pending():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    tracks   = db.get_pending_tracks(user_id)
    playlist = db.get_pending_playlist(user_id)
    result = []
    for t in tracks:
        result.append({
            "title":     t.get("title", "Неизвестный трек"),
            "artist":    t.get("artist", ""),
            "album":     t.get("album", ""),
            "duration":  t.get("duration", 0),
            "audio_url": f"{SERVER_URL}/api/file/{t['file_id']}",
            "file_id":   t.get("file_id", ""),
        })
    return jsonify({"tracks": result, "playlist": playlist})


@flask_app.route("/api/pending/apply", methods=["POST"])
def api_apply_pending():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    body     = request.json or {}
    pl_name  = body.get("playlist", "")
    tracks   = db.get_pending_tracks(user_id)
    for t in tracks:
        db.add_track(user_id, pl_name, t)
    db.clear_pending(user_id)
    return jsonify({"ok": True, "count": len(tracks)})


# ── Прокси для файлов Telegram ──

@flask_app.route("/api/file/<file_id>")
def api_file(file_id):
    """Скачивает файл из Telegram и отдаёт пользователю"""
    try:
        bot  = Bot(BOT_TOKEN)
        loop = asyncio.new_event_loop()

        async def download():
            tg_file = await bot.get_file(file_id)
            path    = f"/tmp/{file_id}"
            await tg_file.download_to_drive(path)
            return path

        path = loop.run_until_complete(download())
        loop.close()

        # Определяем mime type
        if file_id.endswith(".mp3") or True:
            mime = "audio/mpeg"
        return send_file(path, mimetype=mime)
    except Exception as e:
        logger.warning(f"file proxy error: {e}")
        return jsonify({"error": "file not found"}), 404


# ══════════════════════════════════════════
#  Telegram Bot
# ══════════════════════════════════════════

bot_app = None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
       "",
        reply_markup={
            "inline_keyboard": [[{
                "text": "",
                "web_app": {"url": WEBAPP_URL}
            }]]
        }
    )


async def cmd_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    chat_id  = update.effective_chat.id
    pending  = db.get_pending_tracks(user_id)

    if not pending:
        await update.message.reply_text("Нет загруженных треков.")
        return

    count = len(pending)
    await update.message.reply_text(
        f"Загружено треков: {count}\n"
        f"Открой плеер и выбери плейлист для добавления.",
        reply_markup={
            "inline_keyboard": [[{
                "text": "Открыть плеер",
                "web_app": {"url": WEBAPP_URL}
            }]]
        }
    )

    # Очищаем историю чата
    try:
        # Удаляем последние сообщения (до 100)
        # Бот может удалять только свои сообщения и сообщения
        # не старше 48 часов в приватном чате
        pass  # Telegram не позволяет ботам удалять сообщения
              # пользователя в приватных чатах
    except Exception:
        pass


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    audio = update.message.audio
    doc   = update.message.document

    if audio:
        track = {
            "title":    audio.title or "Неизвестный трек",
            "artist":   audio.performer or "",
            "album":    "",
            "duration": audio.duration or 0,
            "file_id":  audio.file_id,
            "thumb_id": audio.thumbnail.file_id if audio.thumbnail else None,
        }
    elif doc:
        mime = doc.mime_type or ""
        if not mime.startswith("audio/"):
            await update.message.reply_text("Это не аудиофайл. Отправьте MP3.")
            return
        track = {
            "title":    doc.file_name or "Неизвестный трек",
            "artist":   "",
            "album":    "",
            "duration": 0,
            "file_id":  doc.file_id,
            "thumb_id": None,
        }
    else:
        return

    db.add_pending_track(user_id, track)
    count = len(db.get_pending_tracks(user_id))

    await update.message.reply_text(
        f"Трек добавлен ({count}). Отправьте ещё или /ready когда закончите."
    )


async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Игнорируем голосовые, кружки, видео"""
    await update.message.reply_text("Принимаю только аудиофайлы (MP3 и подобные).")


def run_bot():
    """Запуск бота в отдельном потоке"""
    global bot_app

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot_app = Application.builder().token(BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("ready", cmd_ready))
    bot_app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_audio))
    bot_app.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION,
        handle_voice_video,
    ))

    logger.info("Bot started!")
    loop.run_until_complete(bot_app.run_polling(drop_pending_updates=True))


# ══════════════════════════════════════════
#  Запуск
# ══════════════════════════════════════════

# Бот в отдельном потоке
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
