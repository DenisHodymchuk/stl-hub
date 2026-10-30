import os
import sqlite3
import threading
import time
import secrets
from typing import Optional, Dict, Any, List

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analytics.db")
_DB_LOCK = threading.RLock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_user_db() -> None:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    google_id TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    avatar_url TEXT DEFAULT '',
                    created_at INTEGER NOT NULL,
                    last_login_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);
                CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

                CREATE TABLE IF NOT EXISTS user_sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON user_sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS user_filaments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    material_type TEXT NOT NULL,
                    brand TEXT DEFAULT '',
                    color_name TEXT DEFAULT '',
                    color_hex TEXT DEFAULT '#3b82f6',
                    price_per_kg REAL NOT NULL,
                    density REAL NOT NULL DEFAULT 1.24,
                    weight_spool_g REAL DEFAULT 1000,
                    remaining_weight_g REAL DEFAULT 1000,
                    nozzle_temp INTEGER DEFAULT 0,
                    bed_temp INTEGER DEFAULT 0,
                    notes TEXT DEFAULT '',
                    is_favorite INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_filaments_user ON user_filaments(user_id);

                CREATE TABLE IF NOT EXISTS user_workshop_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    power_kwh REAL DEFAULT 4.32,
                    printer_watts REAL DEFAULT 180,
                    depreciation REAL DEFAULT 20,
                    prep_fee REAL DEFAULT 20,
                    failure_risk REAL DEFAULT 5,
                    markup_percent REAL DEFAULT 100,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_saved_calculations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    filament_name TEXT DEFAULT '',
                    material_type TEXT DEFAULT '',
                    weight_g REAL DEFAULT 0,
                    print_hours REAL DEFAULT 0,
                    print_minutes REAL DEFAULT 0,
                    quantity INTEGER DEFAULT 1,
                    cost_price REAL DEFAULT 0,
                    client_price REAL DEFAULT 0,
                    details_json TEXT DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_calc_user ON user_saved_calculations(user_id);
                """
            )
            conn.commit()
        finally:
            conn.close()


# ==================== USERS & SESSIONS ====================

SESSION_LIFETIME_SEC = 30 * 86400  # 30 days


def upsert_google_user(google_id: str, email: str, name: str, avatar_url: str = "") -> Dict[str, Any]:
    now = int(time.time())
    with _DB_LOCK:
        conn = _get_conn()
        try:
            # Check if user exists by google_id or email
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE google_id = ? OR email = ?", (google_id, email))
            row = cur.fetchone()
            if row:
                user_id = row["id"]
                conn.execute(
                    """
                    UPDATE users
                    SET google_id = ?, name = ?, avatar_url = ?, last_login_at = ?
                    WHERE id = ?
                    """,
                    (google_id, name or row["name"], avatar_url or row["avatar_url"], now, user_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users (google_id, email, name, avatar_url, created_at, last_login_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (google_id, email, name, avatar_url, now, now),
                )
                user_id = cur.lastrowid

                # Seed default workshop settings for new user
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_workshop_settings (user_id, power_kwh, printer_watts, depreciation, prep_fee, failure_risk, markup_percent, updated_at)
                    VALUES (?, 4.32, 180, 20, 20, 5, 100, ?)
                    """,
                    (user_id, now),
                )

                # Seed 3 starter popular filaments so cabinet looks great immediately!
                starter_filaments = [
                    ("Bambu PLA Basic Black", "PLA", "Bambu Lab", "Чорний", "#1C1C1E", 720.0, 1.24, 1000, 850, 215, 55, "Універсальний пластик для щоденного друку"),
                    ("Plexiwire PETG Signal White", "PETG", "Plexiwire", "Білий", "#F8FAFC", 580.0, 1.27, 1000, 600, 235, 75, "Міцний та вологостійкий"),
                    ("Devil Design TPU 95A Red", "TPU", "Devil Design", "Червоний", "#EF4444", 950.0, 1.21, 1000, 420, 225, 50, "Гнучкий еластомер для прокладок та чохлів"),
                ]
                for f_name, m_type, brand, c_name, c_hex, price, dens, spool_w, rem_w, noz_t, bed_t, notes in starter_filaments:
                    conn.execute(
                        """
                        INSERT INTO user_filaments (
                            user_id, name, material_type, brand, color_name, color_hex,
                            price_per_kg, density, weight_spool_g, remaining_weight_g,
                            nozzle_temp, bed_temp, notes, is_favorite, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (user_id, f_name, m_type, brand, c_name, c_hex, price, dens, spool_w, rem_w, noz_t, bed_t, notes, now, now),
                    )

            conn.commit()
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return dict(cur.fetchone())
        finally:
            conn.close()


def create_user_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    now = int(time.time())
    expires_at = now + SESSION_LIFETIME_SEC
    with _DB_LOCK:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO user_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, now, expires_at),
            )
            # Clean expired sessions occasionally
            conn.execute("DELETE FROM user_sessions WHERE expires_at < ?", (now,))
            conn.commit()
            return token
        finally:
            conn.close()


def get_user_by_session_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or len(token) < 20:
        return None
    now = int(time.time())
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.id, u.google_id, u.email, u.name, u.avatar_url, u.created_at, u.last_login_at
                FROM user_sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token = ? AND s.expires_at > ?
                """,
                (token.strip(), now),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def delete_user_session(token: str) -> None:
    if not token:
        return
    with _DB_LOCK:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM user_sessions WHERE token = ?", (token.strip(),))
            conn.commit()
        finally:
            conn.close()


# ==================== FILAMENTS MANAGEMENT ====================

def get_user_filaments(user_id: int) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM user_filaments
                WHERE user_id = ?
                ORDER BY is_favorite DESC, updated_at DESC, id DESC
                """,
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def add_user_filament(user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    now = int(time.time())
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_filaments (
                    user_id, name, material_type, brand, color_name, color_hex,
                    price_per_kg, density, weight_spool_g, remaining_weight_g,
                    nozzle_temp, bed_temp, notes, is_favorite, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    data.get("name", "Мій пластик").strip(),
                    data.get("material_type", "PLA").strip(),
                    (data.get("brand") or "").strip(),
                    (data.get("color_name") or "").strip(),
                    data.get("color_hex") or "#3b82f6",
                    float(data.get("price_per_kg") or 600.0),
                    float(data.get("density") or 1.24),
                    float(data.get("weight_spool_g") or 1000.0),
                    float(data.get("remaining_weight_g") or 1000.0),
                    int(data.get("nozzle_temp") or 0),
                    int(data.get("bed_temp") or 0),
                    (data.get("notes") or "").strip(),
                    1 if data.get("is_favorite") else 0,
                    now,
                    now,
                ),
            )
            fid = cur.lastrowid
            conn.commit()
            cur.execute("SELECT * FROM user_filaments WHERE id = ?", (fid,))
            return dict(cur.fetchone())
        finally:
            conn.close()


def update_user_filament(user_id: int, filament_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    now = int(time.time())
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE user_filaments
                SET name = ?, material_type = ?, brand = ?, color_name = ?, color_hex = ?,
                    price_per_kg = ?, density = ?, weight_spool_g = ?, remaining_weight_g = ?,
                    nozzle_temp = ?, bed_temp = ?, notes = ?, is_favorite = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    data.get("name", "").strip(),
                    data.get("material_type", "PLA").strip(),
                    (data.get("brand") or "").strip(),
                    (data.get("color_name") or "").strip(),
                    data.get("color_hex") or "#3b82f6",
                    float(data.get("price_per_kg") or 600.0),
                    float(data.get("density") or 1.24),
                    float(data.get("weight_spool_g") or 1000.0),
                    float(data.get("remaining_weight_g") or 1000.0),
                    int(data.get("nozzle_temp") or 0),
                    int(data.get("bed_temp") or 0),
                    (data.get("notes") or "").strip(),
                    1 if data.get("is_favorite") else 0,
                    now,
                    filament_id,
                    user_id,
                ),
            )
            if cur.rowcount == 0:
                return None
            conn.commit()
            cur.execute("SELECT * FROM user_filaments WHERE id = ?", (filament_id,))
            return dict(cur.fetchone())
        finally:
            conn.close()


def delete_user_filament(user_id: int, filament_id: int) -> bool:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM user_filaments WHERE id = ? AND user_id = ?", (filament_id, user_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ==================== WORKSHOP SETTINGS ====================

def get_user_workshop_settings(user_id: int) -> Dict[str, Any]:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM user_workshop_settings WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return {
                "power_kwh": 4.32,
                "printer_watts": 180,
                "depreciation": 20,
                "prep_fee": 20,
                "failure_risk": 5,
                "markup_percent": 100,
            }
        finally:
            conn.close()


def save_user_workshop_settings(user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    now = int(time.time())
    with _DB_LOCK:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT INTO user_workshop_settings (user_id, power_kwh, printer_watts, depreciation, prep_fee, failure_risk, markup_percent, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    power_kwh = excluded.power_kwh,
                    printer_watts = excluded.printer_watts,
                    depreciation = excluded.depreciation,
                    prep_fee = excluded.prep_fee,
                    failure_risk = excluded.failure_risk,
                    markup_percent = excluded.markup_percent,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    float(data.get("power_kwh") or 4.32),
                    float(data.get("printer_watts") or 180),
                    float(data.get("depreciation") or 20),
                    float(data.get("prep_fee") or 20),
                    float(data.get("failure_risk") or 5),
                    float(data.get("markup_percent") or 100),
                    now,
                ),
            )
            conn.commit()
            return get_user_workshop_settings(user_id)
        finally:
            conn.close()


# ==================== SAVED CALCULATIONS ====================

def get_user_calculations(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM user_saved_calculations
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def add_user_calculation(user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    now = int(time.time())
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_saved_calculations (
                    user_id, title, filament_name, material_type, weight_g,
                    print_hours, print_minutes, quantity, cost_price, client_price,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    (data.get("title") or "Розрахунок 3D-друку").strip(),
                    (data.get("filament_name") or "").strip(),
                    (data.get("material_type") or "").strip(),
                    float(data.get("weight_g") or 0),
                    float(data.get("print_hours") or 0),
                    float(data.get("print_minutes") or 0),
                    int(data.get("quantity") or 1),
                    float(data.get("cost_price") or 0),
                    float(data.get("client_price") or 0),
                    data.get("details_json") or "{}",
                    now,
                ),
            )
            cid = cur.lastrowid
            conn.commit()
            cur.execute("SELECT * FROM user_saved_calculations WHERE id = ?", (cid,))
            return dict(cur.fetchone())
        finally:
            conn.close()


def delete_user_calculation(user_id: int, calc_id: int) -> bool:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM user_saved_calculations WHERE id = ? AND user_id = ?", (calc_id, user_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def get_all_users_analytics() -> Dict[str, Any]:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            now = int(time.time())
            day_ago = now - 86400
            week_ago = now - 7 * 86400

            rows = conn.execute(
                """
                SELECT 
                    u.id, u.google_id, u.email, u.name, u.avatar_url, u.created_at, u.last_login_at,
                    (SELECT COUNT(*) FROM user_filaments WHERE user_id = u.id) AS filaments_count,
                    (SELECT COUNT(*) FROM user_saved_calculations WHERE user_id = u.id) AS calculations_count,
                    (SELECT COUNT(*) FROM user_sessions WHERE user_id = u.id AND expires_at > ?) AS active_sessions_count
                FROM users u
                ORDER BY u.last_login_at DESC
                """,
                (now,),
            ).fetchall()

            users_list = [dict(r) for r in rows]
            total_users = len(users_list)
            active_today = sum(1 for u in users_list if (u.get("last_login_at") or 0) >= day_ago)
            active_week = sum(1 for u in users_list if (u.get("last_login_at") or 0) >= week_ago)
            total_filaments = sum(u.get("filaments_count", 0) for u in users_list)
            total_calculations = sum(u.get("calculations_count", 0) for u in users_list)

            return {
                "total_users": total_users,
                "active_today": active_today,
                "active_week": active_week,
                "total_filaments": total_filaments,
                "total_calculations": total_calculations,
                "users": users_list,
            }
        finally:
            conn.close()


# Initialize tables on load
init_user_db()
