# -*- coding: utf-8 -*-
"""
Telegram Music Player — единый backend.
Flask + Telegram Bot API (через requests, без python-telegram-bot).

Запуск на Render:
    gunicorn app:flask_app --bind 0.0.0.0:$PORT --workers 2 --threads 4

Environment Variables (Render):
    BOT_TOKEN   — токен бота от BotFather
    WEBAPP_URL  — https://gussantin01-code.github.io/music-player/
    SERVER_URL  — https://music-player-server-jjkm.onrender.com
    DATA_FILE   — (необязательно) путь к data.json, напр. /data/data.json при диске
"""

import os
import json
import hmac
import time
import hashlib
import logging
import threading
import tempfile
from urllib.parse import parse_qsl, quote

import requests
from flask import Flask, request, jsonify, Response

import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# ─────────────────────────────────────────
# Настройки
# ─────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv(
    "WEBAPP_URL", "https://gussantin01-code.github.io/music-player/"
)
SERVER_URL = (
    os.getenv("SERVER_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
).rstrip("/")

TG_API = "https://api.telegram.org/bot" + BOT_TOKEN
TG_FILE = "https://api.telegram.org/file/bot" + BOT_TOKEN
WEBHOOK_PATH = "/webhook/" + BOT_TOKEN

flask_app = Flask(__name__)


@flask_app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Tg-Init-Data"
    return resp


# ─────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────

def tg(method: str, timeout: int = 30, **payload) -> dict:
    try:
        r = requests.post(TG_API + "/" + method, json=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning("tg.%s failed: %s", method, e)
        return {}


def base_url() -> str:
    return SERVER_URL or request.host_url.rstrip("/")


def track_to_json(t: dict) -> dict:
    t = db.clean_track(t)
    fid = t["file_id"]
    return {
        "title": t["title"],
        "artist": t["artist"],
        "album": t["album"],
        "duration": t["duration"],
        "file_id": fid,
        "audio_url": base_url() + "/api/file/" + quote(fid, safe=""),
        "thumb_url": (
            base_url() + "/api/file/" + quote(t["thumb_id"], safe="")
            if t.get("thumb_id") else None
        ),
    }


def playlist_to_json(p: dict) -> dict:
    return {
        "description": p.get("description") or "",
        "cover": p.get("cover"),
        "tracks": [track_to_json(t) for t in (p.get("tracks") or [])],
    }


# ─────────────────────────────────────────
# Авторизация Mini App (Telegram initData)
# ─────────────────────────────────────────

def validate_init_data(init_data: str):
    """Проверка подписи initData через HMAC. Возвращает dict user или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    check_hash = pairs.pop("hash", None)
    if not check_hash:
        return None
    data_check = "\n".join(
        "%s=%s" % (k, v) for k, v in sorted(pairs.items())
    )
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(
        secret, data_check.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed, check_hash):
        return None
    try:
        return json.loads(pairs.get("user") or "null")
    except Exception:
        return None


def get_user_id_from_request():
    init_data = (
        request.args.get("init_data")
        or request.headers.get("X-Tg-Init-Data")
        or (request.form.get("init_data") if request.form else None)
    )
    body = request.get_json(silent=True) if request.is_json else None
    if not init_data and body:
        init_data = body.get("init_data")
    if init_data:
        user = validate_init_data(init_data)
        if user and user.get("id") is not None:
            return int(user["id"])
    # старый тестовый механизм — для ручной проверки в браузере
    test = request.args.get("test_user") or (body or {}).get("test_user")
    if test is not None:
        try:
            return int(test)
        except (TypeError, ValueError):
            return None
    return None


def require_user():
    uid = get_user_id_from_request()
    if uid is None:
        return None, (jsonify({"error": "unauthorized"}), 401)
    return uid, None


# ─────────────────────────────────────────
# Служебные
# ─────────────────────────────────────────

@flask_app.get("/")
def index():
    return "music-player-server"


@flask_app.get("/ping")
def ping():
    return "pong"


# ─────────────────────────────────────────
# Webhook Telegram
# ─────────────────────────────────────────

@flask_app.post(WEBHOOK_PATH)
def telegram_webhook():
    upd = request.get_json(force=True, silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    if not isinstance(msg, dict) or not msg:
        return "ok"

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_user = msg.get("from") or {}
    user_id = from_user.get("id") or chat_id
    text = (msg.get("text") or "").strip()

    # /start — только кнопка открытия Mini App, без приветственного текста
    if text.startswith("/start"):
        tg(
            "sendMessage",
            chat_id=chat_id,
            text="⁣",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "Открыть плеер",
                     "web_app": {"url": WEBAPP_URL}}
                ]]
            },
        )
        return "ok"

    audio = msg.get("audio")
    doc = msg.get("document") or {}
    is_audio_doc = str(doc.get("mime_type") or "").startswith("audio/")

    # Музыкальный файл → в очередь pending (молча: всё происходит внутри Mini App)
    if audio or (doc and is_audio_doc):
        src = audio or doc
        fname = src.get("file_name") or ""
        title = (
            src.get("title")
            or (fname.rsplit(".", 1)[0] if "." in fname else fname)
            or "Без названия"
        )
        thumb = src.get("thumbnail") or src.get("thumb") or {}
        track = {
            "file_id": src.get("file_id") or "",
            "title": title,
            "artist": src.get("performer") or "",
            "album": "",
            "duration": src.get("duration") or 0,
            "thumb_id": thumb.get("file_id"),
        }
        db.add_pending_track(user_id, track)
        return "ok"

    # Не музыка
    if any(k in msg for k in (
        "voice", "video", "video_note", "animation", "photo", "sticker"
    )):
        tg(
            "sendMessage",
            chat_id=chat_id,
            text="Принимаю только аудиофайлы.",
        )
    return "ok"


# ─────────────────────────────────────────
# Плейлисты
# ─────────────────────────────────────────

@flask_app.get("/api/playlists")
def api_playlists():
    uid, err = require_user()
    if err:
        return err
    playlists = db.get_playlists(uid)
    return jsonify({name: playlist_to_json(p) for name, p in playlists.items()})


@flask_app.post("/api/playlist")
def api_create_playlist():
    uid, err = require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok = db.create_playlist(
        uid, name,
        description=str(body.get("description") or ""),
        cover=body.get("cover"),
    )
    if not ok:
        return jsonify({"error": "exists"}), 409
    return jsonify({"ok": True})


@flask_app.post("/api/playlist/<path:name>/update")
def api_update_playlist(name):
    """Полное сохранение из редактора: название/описание/обложка/треки."""
    uid, err = require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    kwargs = {}
    if "new_name" in body:
        kwargs["new_name"] = str(body.get("new_name") or "").strip()
    if "description" in body:
        kwargs["description"] = str(body.get("description") or "")
    if "cover" in body:
        kwargs["cover"] = body.get("cover")
    if "tracks" in body:
        kwargs["tracks"] = body.get("tracks") or []
    ok, error = db.update_playlist(uid, name, **kwargs)
    if not ok:
        code = 409 if error == "exists" else (404 if error == "not_found" else 400)
        return jsonify({"error": error}), code
    return jsonify({"ok": True})


@flask_app.delete("/api/playlist/<path:name>")
def api_delete_playlist(name):
    uid, err = require_user()
    if err:
        return err
    db.delete_playlist(uid, name)
    return jsonify({"ok": True})


@flask_app.post("/api/playlist/<path:name>/rename")
def api_rename_playlist(name):
    uid, err = require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    new_name = str(body.get("new_name") or "").strip()
    if not new_name:
        return jsonify({"error": "new_name required"}), 400
    ok = db.rename_playlist(uid, name, new_name)
    return jsonify({"ok": ok})


@flask_app.post("/api/playlist/<path:name>/add_track")
def api_add_track(name):
    uid, err = require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    track = body.get("track")
    if not track:
        return jsonify({"error": "track required"}), 400
    ok = db.add_track(uid, name, track)
    return jsonify({"ok": ok})


@flask_app.delete("/api/playlist/<path:name>/track")
def api_remove_track(name):
    uid, err = require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        index = int(body.get("index", -1))
    except (TypeError, ValueError):
        index = -1
    ok = db.remove_track(uid, name, index)
    return jsonify({"ok": ok})


# ─────────────────────────────────────────
# Pending (очередь от бота)
# ─────────────────────────────────────────

@flask_app.get("/api/pending")
def api_pending():
    uid, err = require_user()
    if err:
        return err
    tracks = db.get_pending_tracks(uid)
    return jsonify({"tracks": [track_to_json(t) for t in tracks]})


@flask_app.post("/api/pending/apply")
def api_pending_apply():
    uid, err = require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    playlist = str(body.get("playlist") or "").strip()
    if not playlist:
        return jsonify({"error": "playlist required"}), 400
    added = db.apply_pending_to_playlist(uid, playlist)
    return jsonify({"ok": True, "added": added})


# ─────────────────────────────────────────
# Каталог («Все треки»)
# ─────────────────────────────────────────

@flask_app.get("/api/catalog")
def api_catalog():
    uid, err = require_user()
    if err:
        return err
    return jsonify([track_to_json(t) for t in db.get_catalog(uid)])


@flask_app.post("/api/catalog/load")
def api_catalog_load():
    """«Загрузить треки»: перенести очередь от бота в общий каталог."""
    uid, err = require_user()
    if err:
        return err
    added, catalog = db.load_pending_to_catalog(uid)
    return jsonify({
        "ok": True,
        "added": added,
        "catalog": [track_to_json(t) for t in catalog],
    })


@flask_app.delete("/api/catalog/<path:file_id>")
def api_catalog_remove(file_id):
    uid, err = require_user()
    if err:
        return err
    ok = db.remove_from_catalog(uid, file_id)
    return jsonify({"ok": ok})


# ─────────────────────────────────────────
# Загрузка аудио с телефона (из Mini App)
# ─────────────────────────────────────────

@flask_app.post("/api/upload/audio")
def api_upload_audio():
    uid, err = require_user()
    if err:
        return err
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400

    raw_name = f.filename
    title = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
    suffix = ("." + raw_name.rsplit(".", 1)[1]) if "." in raw_name else ".mp3"
    mime = f.mimetype or "audio/mpeg"

    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        f.save(tmp)
        # Отправляем файл пользователю через бота, чтобы получить file_id
        with open(tmp, "rb") as fh:
            r = requests.post(
                TG_API + "/sendAudio",
                data={"chat_id": str(uid), "title": title},
                files={"audio": (raw_name, fh, mime)},
                timeout=300,
            )
        res = r.json()
        # если Telegram не распознал как audio — шлём как документ
        if not res.get("ok"):
            with open(tmp, "rb") as fh:
                r = requests.post(
                    TG_API + "/sendDocument",
                    data={"chat_id": str(uid)},
                    files={"document": (raw_name, fh, mime)},
                    timeout=300,
                )
            res = r.json()
    except Exception as e:
        log.warning("upload error: %s", e)
        return jsonify({"error": "upload failed"}), 502
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not res.get("ok"):
        log.warning("telegram upload failed: %s", res)
        return jsonify({"error": "telegram rejected file"}), 502

    result = res.get("result") or {}
    a = result.get("audio") or result.get("document") or {}
    thumb = a.get("thumbnail") or a.get("thumb") or {}
    track = {
        "file_id": a.get("file_id") or "",
        "title": a.get("title") or title,
        "artist": a.get("performer") or "",
        "album": "",
        "duration": a.get("duration") or 0,
        "thumb_id": thumb.get("file_id"),
    }
    db.add_pending_track(uid, track)
    return jsonify({"ok": True, "track": track_to_json(track)})


# ─────────────────────────────────────────
# Прокси телеграм-файлов (стриминг + Range)
# ─────────────────────────────────────────

@flask_app.get("/api/file/<path:file_id>")
def api_file(file_id):
    try:
        r = requests.get(
            TG_API + "/getFile", params={"file_id": file_id}, timeout=20
        )
        info = r.json()
    except Exception:
        info = {}
    if not info.get("ok"):
        return jsonify({"error": "file not found"}), 404

    fpath = (info.get("result") or {}).get("file_path")
    if not fpath:
        return jsonify({"error": "file not found"}), 404

    upstream_headers = {}
    rng = request.headers.get("Range")
    if rng:
        upstream_headers["Range"] = rng
    try:
        up = requests.get(
            TG_FILE + "/" + fpath, headers=upstream_headers,
            stream=True, timeout=120,
        )
    except Exception as e:
        log.warning("file proxy error: %s", e)
        return jsonify({"error": "upstream error"}), 502

    out_headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    for h in ("Content-Type", "Content-Length", "Content-Range"):
        if up.headers.get(h):
            out_headers[h] = up.headers[h]
    if "Content-Type" not in out_headers:
        out_headers["Content-Type"] = "audio/mpeg"

    return Response(
        up.iter_content(64 * 1024),
        status=up.status_code,
        headers=out_headers,
    )


# ─────────────────────────────────────────
# Установка webhook при старте
# ─────────────────────────────────────────

def setup_webhook():
    time.sleep(2)
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is not set!")
        return
    if not SERVER_URL:
        log.warning("SERVER_URL is not set — webhook не будет установлен")
        return
    try:
        r = requests.get(
            TG_API + "/setWebhook",
            params={
                "url": SERVER_URL + WEBHOOK_PATH,
                "drop_pending_updates": "true",
                "allowed_updates": json.dumps(["message", "edited_message"]),
            },
            timeout=20,
        )
        log.info("Webhook set: %s", r.text)
    except Exception as e:
        log.warning("Webhook error: %s", e)
    log.info("Bot started!")


threading.Thread(target=setup_webhook, daemon=True).start()
