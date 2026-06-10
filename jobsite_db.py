import sqlite3
import math
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "jobsite.db")


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS job_sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            radius_m REAL DEFAULT 150,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            last_visited TEXT
        )""")


def _dist_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def find_nearby(lat, lon, radius_m=200):
    rows = []
    with sqlite3.connect(DB_PATH) as c:
        for r in c.execute(
            "SELECT id,name,lat,lon,radius_m,notes,last_visited FROM job_sites"
        ):
            d = _dist_m(lat, lon, r[2], r[3])
            if d <= max(radius_m, r[4]):
                rows.append({
                    "id": r[0], "name": r[1], "lat": r[2], "lon": r[3],
                    "radius_m": r[4], "notes": r[5], "last_visited": r[6], "dist": d,
                })
    rows.sort(key=lambda x: x["dist"])
    return rows


def add_site(name, lat, lon, notes=""):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO job_sites (name,lat,lon,notes,created_at) VALUES (?,?,?,?,?)",
            (name, lat, lon, notes, now),
        )
        return cur.lastrowid


def append_note(site_id, note):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT notes FROM job_sites WHERE id=?", (site_id,)).fetchone()
        if not row:
            return False
        existing = (row[0] or "").rstrip()
        updated = (existing + "\n" if existing else "") + f"[{today}] {note}"
        c.execute("UPDATE job_sites SET notes=? WHERE id=?", (updated, site_id))
    return True


def update_last_visited(site_id):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE job_sites SET last_visited=? WHERE id=?",
            (datetime.utcnow().isoformat(), site_id),
        )


def list_sites():
    with sqlite3.connect(DB_PATH) as c:
        return [
            {"id": r[0], "name": r[1], "lat": r[2], "lon": r[3],
             "notes": r[4], "last_visited": r[5]}
            for r in c.execute(
                "SELECT id,name,lat,lon,notes,last_visited FROM job_sites "
                "ORDER BY COALESCE(last_visited,'') DESC, created_at DESC"
            )
        ]


def get_site(site_id):
    with sqlite3.connect(DB_PATH) as c:
        r = c.execute(
            "SELECT id,name,lat,lon,radius_m,notes,last_visited FROM job_sites WHERE id=?",
            (site_id,),
        ).fetchone()
        if r:
            return {"id": r[0], "name": r[1], "lat": r[2], "lon": r[3],
                    "radius_m": r[4], "notes": r[5], "last_visited": r[6]}
    return None


def rename_site(site_id, new_name):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE job_sites SET name=? WHERE id=?", (new_name, site_id))


def delete_site(site_id):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM job_sites WHERE id=?", (site_id,))
