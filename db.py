# -*- coding: utf-8 -*-
"""
Хранилище данных Telegram Music Player.
Простая JSON-база: плейлисты (с описанием и обложкой), очередь pending, общий каталог.
"""

import json
import os
import threading

DB_FILE = os.environ.get("DATA_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data.json"
)
_lock = threading.Lock()

# Маркер «аргумент не передан» — чтобы отличать None от «не трогать поле»
_MISSING = object()


# ─────────────────────────────────────────
# Низкий уровень
# ─────────────────────────────────────────

def _load_all() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _save_all(data: dict):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)


def clean_track(t: dict) -> dict:
    """Привести трек к базовому виду (без временных url-полей от frontend)."""
    t = t or {}
    try:
        duration = int(t.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "file_id": t.get("file_id") or "",
        "title": t.get("title") or "Без названия",
        "artist": t.get("artist") or "",
        "album": t.get("album") or "",
        "duration": duration,
        "thumb_id": t.get("thumb_id") or None,
    }


def _migrate_playlists(playlists: dict) -> dict:
    """Поддержка старых форматов: список треков напрямую / cover_file_id."""
    out = {}
    for name, p in (playlists or {}).items():
        if isinstance(p, list):
            p = {"tracks": p}
        if not isinstance(p, dict):
            p = {}
        cover = p.get("cover")
        if cover is None and p.get("cover_file_id"):
            # старая обложка через telegram file_id — отдаём через прокси
            cover = "/api/file/" + str(p["cover_file_id"])
        out[name] = {
            "description": str(p.get("description") or ""),
            "cover": cover,
            "tracks": [clean_track(t) for t in (p.get("tracks") or [])],
        }
    return out


def get_user(user_id: int) -> dict:
    with _lock:
        all_data = _load_all()
        key = str(user_id)
        if not isinstance(all_data.get(key), dict):
            all_data[key] = {}
        user = all_data[key]
        user["playlists"] = _migrate_playlists(user.get("playlists"))
        user["pending_tracks"] = [
            clean_track(t) for t in (user.get("pending_tracks") or []) if t
        ]
        user["catalog"] = [
            clean_track(t) for t in (user.get("catalog") or []) if t
        ]
        all_data[key] = user
        _save_all(all_data)
        return user


def save_user(user_id: int, user_data: dict):
    with _lock:
        all_data = _load_all()
        all_data[str(user_id)] = user_data
        _save_all(all_data)


# ─────────────────────────────────────────
# Плейлисты
# ─────────────────────────────────────────

def get_playlists(user_id: int) -> dict:
    return get_user(user_id)["playlists"]


def create_playlist(user_id: int, name: str, description: str = "", cover=None) -> bool:
    user = get_user(user_id)
    if name in user["playlists"]:
        return False
    user["playlists"][name] = {
        "description": description or "",
        "cover": cover,
        "tracks": [],
    }
    save_user(user_id, user)
    return True


def delete_playlist(user_id: int, name: str) -> bool:
    user = get_user(user_id)
    if name in user["playlists"]:
        del user["playlists"][name]
        save_user(user_id, user)
        return True
    return False


def rename_playlist(user_id: int, old_name: str, new_name: str) -> bool:
    user = get_user(user_id)
    pls = user["playlists"]
    if old_name not in pls or new_name in pls:
        return False
    # пересобираем словарь, сохраняя порядок
    user["playlists"] = {
        (new_name if k == old_name else k): v for k, v in pls.items()
    }
    save_user(user_id, user)
    return True


def update_playlist(user_id: int, name: str,
                    new_name=_MISSING, description=_MISSING,
                    cover=_MISSING, tracks=_MISSING):
    """
    Полное обновление плейлиста (экран редактирования).
    Поля, которые не переданы, остаются как были.
    Возвращает (ok: bool, error: str).
    """
    user = get_user(user_id)
    pls = user["playlists"]
    if name not in pls:
        return False, "not_found"

    final_name = name
    if new_name is not _MISSING:
        nn = str(new_name or "").strip()
        if not nn:
            return False, "name_required"
        if nn != name and nn in pls:
            return False, "exists"
        if nn != name:
            user["playlists"] = {
                (nn if k == name else k): v for k, v in pls.items()
            }
            pls = user["playlists"]
            final_name = nn

    item = pls[final_name]
    if description is not _MISSING:
        item["description"] = str(description or "")
    if cover is not _MISSING:
        item["cover"] = cover
    if tracks is not _MISSING:
        item["tracks"] = [clean_track(t) for t in (tracks or [])]

    save_user(user_id, user)
    return True, ""


# ─────────────────────────────────────────
# Треки внутри плейлиста
# ─────────────────────────────────────────

def add_track(user_id: int, playlist_name: str, track: dict) -> bool:
    user = get_user(user_id)
    pl = user["playlists"].get(playlist_name)
    if pl is None:
        return False
    pl["tracks"].append(clean_track(track))
    save_user(user_id, user)
    return True


def remove_track(user_id: int, playlist_name: str, track_index: int) -> bool:
    user = get_user(user_id)
    pl = user["playlists"].get(playlist_name)
    if pl is None:
        return False
    tracks = pl["tracks"]
    if 0 <= track_index < len(tracks):
        tracks.pop(track_index)
        save_user(user_id, user)
        return True
    return False


# ─────────────────────────────────────────
# Pending (очередь от бота)
# ─────────────────────────────────────────

def add_pending_track(user_id: int, track: dict) -> bool:
    """False — такой file_id уже есть в очереди или каталоге (дубликат)."""
    track = clean_track(track)
    if not track["file_id"]:
        return False
    user = get_user(user_id)
    known = {t["file_id"] for t in user["pending_tracks"]}
    known |= {t["file_id"] for t in user["catalog"]}
    if track["file_id"] in known:
        return False
    user["pending_tracks"].append(track)
    save_user(user_id, user)
    return True


def get_pending_tracks(user_id: int) -> list:
    return get_user(user_id)["pending_tracks"]


def clear_pending(user_id: int):
    user = get_user(user_id)
    user["pending_tracks"] = []
    save_user(user_id, user)


def apply_pending_to_playlist(user_id: int, playlist_name: str) -> int:
    """Старый сценарий: слить всю очередь в конкретный плейлист."""
    user = get_user(user_id)
    pl = user["playlists"].get(playlist_name)
    if pl is None:
        return 0
    have = {t["file_id"] for t in pl["tracks"]}
    added = 0
    for t in user["pending_tracks"]:
        if t["file_id"] not in have:
            pl["tracks"].append(t)
            have.add(t["file_id"])
            added += 1
    user["pending_tracks"] = []
    save_user(user_id, user)
    return added


# ─────────────────────────────────────────
# Каталог («Все треки»)
# ─────────────────────────────────────────

def get_catalog(user_id: int) -> list:
    return get_user(user_id)["catalog"]


def add_to_catalog(user_id: int, track: dict) -> bool:
    track = clean_track(track)
    if not track["file_id"]:
        return False
    user = get_user(user_id)
    ids = {t["file_id"] for t in user["catalog"]}
    if track["file_id"] in ids:
        return False
    user["catalog"].append(track)
    save_user(user_id, user)
    return True


def load_pending_to_catalog(user_id: int) -> tuple:
    """
    Перенести очередь pending в каталог (без дублей по file_id).
    Pending после этого очищается.
    Возвращает (кол-во новых, весь каталог).
    """
    user = get_user(user_id)
    ids = {t["file_id"] for t in user["catalog"]}
    added = 0
    for t in user["pending_tracks"]:
        if t["file_id"] not in ids:
            user["catalog"].append(t)
            ids.add(t["file_id"])
            added += 1
    user["pending_tracks"] = []
    save_user(user_id, user)
    return added, user["catalog"]


def remove_from_catalog(user_id: int, file_id: str) -> bool:
    user = get_user(user_id)
    before = len(user["catalog"])
    user["catalog"] = [t for t in user["catalog"] if t["file_id"] != file_id]
    if len(user["catalog"]) < before:
        save_user(user_id, user)
        return True
    return False
