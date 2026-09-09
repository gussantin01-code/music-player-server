import os
import logging
import asyncio
import hashlib
import hmac
import json
from urllib.parse import parse_qs

from flask import Flask, request, jsonify, send_file, Response
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
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
SERVER_URL = os.getenv("SERVER_URL", "")
WEBHOOK_PATH = "/webhook/" + BOT_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════
#  Flask
# ══════════════════════════════════════════
flask_app = Flask(__name__)
CORS(flask_app)

# ══════════════════════════════════════════
#  Telegram Application (глобально)
# ══════════════════════════════════════════
bot_application: Application = None


def get_loop():
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


# ══════════════════════════════════════════
#  Обработчики бота
# ══════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\u2063",
        reply_markup={
            "inline_keyboard": [[{
                "text": "Открыть плеер",
                "web_app": {"url": WEBAPP_URL}
            }]]
        }
    )


async def cmd_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending = db.get_pending_tracks(user_id)

    if not pending:
        await update.message.reply_text("Нет загруженных треков.")
        return

    count = len(pending)
    await update.message.reply_text(
        f"Загружено треков: {count}\n"
        f"Открой плеер и выбери плейлист.",
        reply_markup={
            "inline_keyboard": [[{
                "text": "Открыть плеер",
                "web_app": {"url": WEBAPP_URL}
            }]]
        }
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    audio   = update.message.audio
    doc     = update.message.document

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
    await update.message.reply_text("Принимаю только аудиофайлы (MP3).")


# ══════════════════════════════════════════
#  Flask роуты
# ══════════════════════════════════════════
@flask_app.route("/ping")
def ping():
    return "pong"


@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    """Принимает апдейты от Telegram"""
    data = request.get_json(force=True)
    loop = get_loop()

    async def process():
        update = Update.de_json(data, bot_application.bot)
        await bot_application.process_update(update)

    loop.run_until_complete(process())
    return Response("ok", status=200)


# ══════════════════════════════════════════
#  Auth helper
# ══════════════════════════════════════════
def validate_init_data(init_data: str):
    try:
        parsed     = parse_qs(init_data)
        check_hash = parsed.get("hash", [None])[0]
        if not check_hash:
            return None
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
        return json.loads(user_json) if user_json else None
    except Exception as e:
        logger.warning(f"validate_init_data: {e}")
        return None


def get_user_id_from_request():
    init_data = request.args.get("init_data") or request.form.get("init_data")
    if init_data:
        user = validate_init_data(init_data)
        if user:
            return user.get("id")
    uid = request.args.get("user_id")
    if uid:
        return int(uid)
    return None


# ══════════════════════════════════════════
#  API роуты
# ══════════════════════════════════════════
@flask_app.route("/api/playlists")
def api_playlists():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    playlists = db.get_playlists(user_id)
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
    body     = request.json or {}
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
    body  = request.json or {}
    index = body.get("index", -1)
    ok    = db.remove_track(user_id, name, index)
    return jsonify({"ok": ok})


@flask_app.route("/api/pending")
def api_pending():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    tracks   = db.get_pending_tracks(user_id)
    playlist = db.get_pending_playlist(user_id)
    result   = []
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
    body    = request.json or {}
    pl_name = body.get("playlist", "")
    tracks  = db.get_pending_tracks(user_id)
    for t in tracks:
        db.add_track(user_id, pl_name, t)
    db.clear_pending(user_id)
    return jsonify({"ok": True, "count": len(tracks)})


@flask_app.route("/api/file/<file_id>")
def api_file(file_id):
    try:
        bot  = Bot(BOT_TOKEN)
        loop = get_loop()

        async def download():
            tg_file = await bot.get_file(file_id)
            path    = f"/tmp/{file_id}"
            await tg_file.download_to_drive(path)
            return path

        path = loop.run_until_complete(download())
        return send_file(path, mimetype="audio/mpeg")
    except Exception as e:
        logger.warning(f"file proxy error: {e}")
        return jsonify({"error": "file not found"}), 404


# ══════════════════════════════════════════
#  Инициализация бота (вебхук)
# ══════════════════════════════════════════
def setup_bot():
    global bot_application
    loop = get_loop()

    async def _setup():
        global bot_application
        bot_application = Application.builder().token(BOT_TOKEN).build()

        bot_application.add_handler(CommandHandler("start", cmd_start))
        bot_application.add_handler(CommandHandler("ready", cmd_ready))
        bot_application.add_handler(MessageHandler(
            filters.AUDIO | filters.Document.AUDIO, handle_audio
        ))
        bot_application.add_handler(MessageHandler(
            filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION,
            handle_voice_video,
        ))

        await bot_application.initialize()

        # Устанавливаем вебхук
        webhook_url = SERVER_URL + WEBHOOK_PATH
        await bot_application.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set: {webhook_url}")

    loop.run_until_complete(_setup())
    logger.info("Bot started!")


# Запускаем при старте
setup_bot()

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
