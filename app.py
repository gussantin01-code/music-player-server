import os
import json
import hmac
import hashlib
import asyncio
import logging
import time
from urllib.parse import parse_qs

from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
SERVER_URL = os.getenv("SERVER_URL", "")

WEBHOOK_PATH = "/webhook/" + BOT_TOKEN

flask_app = Flask(__name__)
CORS(flask_app)

tg_app: Application | None = None


def get_loop():
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        l = asyncio.new_event_loop()
        asyncio.set_event_loop(l)
        return l


def validate_init_data(init_data: str):
    try:
        parsed = parse_qs(init_data)
        check_hash = parsed.get("hash", [None])[0]
        if not check_hash:
            return None
        data_check = []
        for key in sorted(parsed.keys()):
            if key == "hash":
                continue
            data_check.append(f"{key}={parsed[key][0]}")
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        computed = hmac.new(
            secret_key, "\n".join(data_check).encode(), hashlib.sha256
        ).hexdigest()
        if computed != check_hash:
            return None
        user_json = parsed.get("user", [None])[0]
        return json.loads(user_json) if user_json else None
    except Exception:
        return None


def get_user_id_from_request():
    init_data = request.args.get("init_data") or request.form.get("init_data")
    if init_data:
        user = validate_init_data(init_data)
        if user:
            return int(user.get("id"))
    uid = request.args.get("user_id")
    return int(uid) if uid else None


def track_to_json(t: dict) -> dict:
    return {
        "title":     t.get("title", "Track"),
        "artist":    t.get("artist", ""),
        "album":     t.get("album", ""),
        "duration":  t.get("duration", 0),
        "audio_url": f"{SERVER_URL}/api/file/{t['file_id']}",
        "thumb_url": f"{SERVER_URL}/api/file/{t['thumb_id']}" if t.get("thumb_id") else None,
        "file_id":   t.get("file_id", ""),
        "thumb_id":  t.get("thumb_id"),
    }


# ─────────────────────────────────────────
# Flask роуты
# ─────────────────────────────────────────

@flask_app.get("/ping")
def ping():
    return "pong"


@flask_app.post(WEBHOOK_PATH)
def webhook():
    data = request.get_json(force=True)
    l = get_loop()

    async def run():
        upd = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(upd)

    l.run_until_complete(run())
    return Response("ok", 200)


# ── Плейлисты ──

@flask_app.get("/api/playlists")
def api_playlists():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401

    playlists = db.get_playlists(user_id)
    out = {}
    for name, p in playlists.items():
        out[name] = {
            "cover_url": f"{SERVER_URL}/api/file/{p['cover_file_id']}" if p.get("cover_file_id") else None,
            "tracks": [track_to_json(t) for t in p.get("tracks", [])],
        }
    return jsonify(out)


@flask_app.post("/api/playlist")
def api_create_playlist():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok = db.create_playlist(user_id, name)
    if not ok:
        return jsonify({"error": "already exists"}), 409
    return jsonify({"ok": True})


@flask_app.delete("/api/playlist/<name>")
def api_delete_playlist(name):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    db.delete_playlist(user_id, name)
    return jsonify({"ok": True})


@flask_app.post("/api/playlist/<name>/rename")
def api_rename_playlist(name):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    body = request.json or {}
    new_name = (body.get("new_name") or "").strip()
    if not new_name:
        return jsonify({"error": "new_name required"}), 400
    ok = db.rename_playlist(user_id, name, new_name)
    return jsonify({"ok": ok})


@flask_app.post("/api/playlist/<name>/add_track")
def api_add_track(name):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    body = request.json or {}
    track = body.get("track")
    if not track:
        return jsonify({"error": "track required"}), 400
    ok = db.add_track(user_id, name, track)
    return jsonify({"ok": ok})


@flask_app.delete("/api/playlist/<name>/track")
def api_remove_track(name):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    body = request.json or {}
    index = int(body.get("index", -1))
    ok = db.remove_track(user_id, name, index)
    return jsonify({"ok": ok})


# ── Каталог ──

@flask_app.get("/api/catalog")
def api_catalog():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    tracks = db.get_catalog(user_id)
    return jsonify([track_to_json(t) for t in tracks])


@flask_app.post("/api/catalog/load")
def api_catalog_load():
    """Перенести все pending треки из бота в каталог"""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    added = db.load_pending_to_catalog(user_id)
    return jsonify({"ok": True, "added": added})


@flask_app.delete("/api/catalog/<file_id>")
def api_catalog_remove(file_id):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    ok = db.remove_from_catalog(user_id, file_id)
    return jsonify({"ok": ok})


# ── Pending (для бота) ──

@flask_app.get("/api/pending")
def api_pending():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    tracks = db.get_pending_tracks(user_id)
    return jsonify({"tracks": [track_to_json(t) for t in tracks]})


# ── Файловый прокси ──

@flask_app.get("/api/file/<file_id>")
def api_file(file_id):
    bot = Bot(BOT_TOKEN)
    l = get_loop()

    async def dl():
        f = await bot.get_file(file_id)
        path = f"/tmp/{file_id}"
        await f.download_to_drive(path)
        return path

    try:
        path = l.run_until_complete(dl())
        return send_file(path)
    except Exception:
        return jsonify({"error": "file"}), 404


# ─────────────────────────────────────────
# Bot handlers
# ─────────────────────────────────────────

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


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    a = update.message.audio
    d = update.message.document

    if a:
        track = {
            "title":    a.title or "Track",
            "artist":   a.performer or "",
            "album":    "",
            "duration": a.duration or 0,
            "file_id":  a.file_id,
            "thumb_id": a.thumbnail.file_id if a.thumbnail else None,
        }
    elif d and (d.mime_type or "").startswith("audio/"):
        track = {
            "title":    d.file_name or "Track",
            "artist":   "",
            "album":    "",
            "duration": 0,
            "file_id":  d.file_id,
            "thumb_id": None,
        }
    else:
        return

    db.add_pending_track(user_id, track)
    # тихо — не отвечаем, чтобы не засорять чат


def setup_webhook():
    global tg_app
    l = get_loop()

    async def init():
        global tg_app
        tg_app = Application.builder().token(BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", cmd_start))
        tg_app.add_handler(MessageHandler(
            filters.AUDIO | filters.Document.AUDIO, handle_audio
        ))
        await tg_app.initialize()
        await tg_app.bot.set_webhook(SERVER_URL + WEBHOOK_PATH)

    l.run_until_complete(init())
    log.info("Bot started!")


setup_webhook()
