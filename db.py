import json
import os
import threading

DB_FILE = os.path.join(os.path.dirname(__file__), "data.json")
_lock = threading.Lock()


def _load_all() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_all(data: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _user_key(user_id: int) -> str:
    return str(user_id)


def get_user(user_id: int) -> dict:
    with _lock:
        all_data = _load_all()
        key = _user_key(user_id)
        if key not in all_data:
            all_data[key] = {
                "playlists": {},
                "pending_tracks": [],
                "catalog": [],
            }
            _save_all(all_data)
        user = all_data[key]
        # migrate old data
        if "catalog" not in user:
            user["catalog"] = []
            all_data[key] = user
            _save_all(all_data)
        return user


def save_user(user_id: int, user_data: dict):
    with _lock:
        all_data = _load_all()
        all_data[_user_key(user_id)] = user_data
        _save_all(all_data)


# ── Плейлисты ──

def get_playlists(user_id: int) -> dict:
    return get_user(user_id).get("playlists", {})


def create_playlist(user_id: int, name: str, cover_file_id: str = None) -> bool:
    user = get_user(user_id)
    if name in user["playlists"]:
        return False
    user["playlists"][name] = {"cover_file_id": cover_file_id, "tracks": []}
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
    if old_name not in user["playlists"]:
        return False
    if new_name in user["playlists"]:
        return False
    user["playlists"][new_name] = user["playlists"].pop(old_name)
    save_user(user_id, user)
    return True


def update_playlist_cover(user_id: int, name: str, cover_file_id: str):
    user = get_user(user_id)
    if name in user["playlists"]:
        user["playlists"][name]["cover_file_id"] = cover_file_id
        save_user(user_id, user)


# ── Треки ──

def add_track(user_id: int, playlist_name: str, track: dict) -> bool:
    user = get_user(user_id)
    if playlist_name not in user["playlists"]:
        return False
    user["playlists"][playlist_name]["tracks"].append(track)
    save_user(user_id, user)
    return True


def remove_track(user_id: int, playlist_name: str, track_index: int) -> bool:
    user = get_user(user_id)
    if playlist_name not in user["playlists"]:
        return False
    tracks = user["playlists"][playlist_name]["tracks"]
    if 0 <= track_index < len(tracks):
        tracks.pop(track_index)
        save_user(user_id, user)
        return True
    return False


def get_tracks(user_id: int, playlist_name: str) -> list:
    user = get_user(user_id)
    pl = user["playlists"].get(playlist_name)
    return pl.get("tracks", []) if pl else []


# ── Pending ──

def add_pending_track(user_id: int, track: dict):
    user = get_user(user_id)
    user["pending_tracks"].append(track)
    save_user(user_id, user)


def get_pending_tracks(user_id: int) -> list:
    return get_user(user_id).get("pending_tracks", [])


def clear_pending(user_id: int):
    user = get_user(user_id)
    user["pending_tracks"] = []
    save_user(user_id, user)


# ── Каталог ──

def get_catalog(user_id: int) -> list:
    return get_user(user_id).get("catalog", [])


def add_to_catalog(user_id: int, track: dict):
    """Добавить трек в каталог (без дублей по file_id)"""
    user = get_user(user_id)
    existing_ids = {t["file_id"] for t in user["catalog"]}
    if track["file_id"] not in existing_ids:
        user["catalog"].append(track)
        save_user(user_id, user)


def load_pending_to_catalog(user_id: int) -> int:
    """Перенести все pending треки в каталог, очистить pending. Вернуть кол-во добавленных."""
    user = get_user(user_id)
    pending = user.get("pending_tracks", [])
    existing_ids = {t["file_id"] for t in user.get("catalog", [])}
    added = 0
    for t in pending:
        if t["file_id"] not in existing_ids:
            user["catalog"].append(t)
            existing_ids.add(t["file_id"])
            added += 1
    user["pending_tracks"] = []
    save_user(user_id, user)
    return added


def remove_from_catalog(user_id: int, file_id: str) -> bool:
    user = get_user(user_id)
    before = len(user["catalog"])
    user["catalog"] = [t for t in user["catalog"] if t["file_id"] != file_id]
    if len(user["catalog"]) < before:
        save_user(user_id, user)
        return True
    return False
