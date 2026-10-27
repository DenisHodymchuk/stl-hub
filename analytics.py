from datetime import datetime, timedelta
import hashlib
import os
import sqlite3
import threading
import time
from typing import Dict, Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analytics.db")
_DB_LOCK = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _DB_LOCK:
        conn = _get_conn()
        try:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS page_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    date_str TEXT NOT NULL,
                    visitor_hash TEXT NOT NULL,
                    ip_masked TEXT,
                    device TEXT,
                    referrer TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pv_date ON page_views(date_str);
                CREATE INDEX IF NOT EXISTS idx_pv_ts ON page_views(ts);

                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    date_str TEXT NOT NULL,
                    visitor_hash TEXT NOT NULL,
                    search_type TEXT NOT NULL,
                    query TEXT NOT NULL,
                    ai_title TEXT,
                    results_count INTEGER DEFAULT 0,
                    elapsed_sec REAL DEFAULT 0.0
                );
                CREATE INDEX IF NOT EXISTS idx_srch_date ON searches(date_str);
                CREATE INDEX IF NOT EXISTS idx_srch_ts ON searches(ts);

                CREATE TABLE IF NOT EXISTS outbound_clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    date_str TEXT NOT NULL,
                    visitor_hash TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    model_title TEXT,
                    model_url TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_clk_date ON outbound_clicks(date_str);
                """
            )
            conn.commit()
        finally:
            conn.close()


def make_visitor_info(ip: str, user_agent: str) -> tuple[str, str, str]:
    raw = f"{ip}|{user_agent}"
    v_hash = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    parts = (ip or "").split(".")
    ip_masked = f"{parts[0]}.{parts[1]}.{parts[2]}.***" if len(parts) == 4 else (ip[:12] if ip else "unknown")
    ua_low = (user_agent or "").lower()
    if any(k in ua_low for k in ("iphone", "android", "mobile", "ipad")):
        device = "📱 Мобільний"
    else:
        device = "💻 ПК"
    return v_hash, ip_masked, device


def log_page_view(ip: str, user_agent: str, referrer: str = "") -> None:
    try:
        now = int(time.time())
        date_str = datetime.now().strftime("%Y-%m-%d")
        v_hash, ip_masked, device = make_visitor_info(ip, user_agent)
        with _DB_LOCK:
            conn = _get_conn()
            try:
                conn.execute(
                    "INSERT INTO page_views (ts, date_str, visitor_hash, ip_masked, device, referrer) VALUES (?, ?, ?, ?, ?, ?)",
                    (now, date_str, v_hash, ip_masked, device, (referrer or "")[:200]),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"[Analytics PageView Error] {e}")


def log_search_event(
    ip: str,
    user_agent: str,
    search_type: str,
    query: str,
    ai_title: str = "",
    results_count: int = 0,
    elapsed_sec: float = 0.0,
) -> None:
    try:
        now = int(time.time())
        date_str = datetime.now().strftime("%Y-%m-%d")
        v_hash, _, _ = make_visitor_info(ip, user_agent)
        with _DB_LOCK:
            conn = _get_conn()
            try:
                conn.execute(
                    "INSERT INTO searches (ts, date_str, visitor_hash, search_type, query, ai_title, results_count, elapsed_sec) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        date_str,
                        v_hash,
                        search_type,
                        (query or "").strip()[:150],
                        (ai_title or "").strip()[:150],
                        int(results_count or 0),
                        float(elapsed_sec or 0.0),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"[Analytics Search Error] {e}")


def log_click_event(ip: str, user_agent: str, site_id: str, model_title: str = "", model_url: str = "") -> None:
    try:
        now = int(time.time())
        date_str = datetime.now().strftime("%Y-%m-%d")
        v_hash, _, _ = make_visitor_info(ip, user_agent)
        with _DB_LOCK:
            conn = _get_conn()
            try:
                conn.execute(
                    "INSERT INTO outbound_clicks (ts, date_str, visitor_hash, site_id, model_title, model_url) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        date_str,
                        v_hash,
                        (site_id or "other").strip().lower()[:40],
                        (model_title or "").strip()[:200],
                        (model_url or "").strip()[:400],
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"[Analytics Click Error] {e}")


def get_dashboard_stats(days: int = 14) -> Dict[str, Any]:
    now = int(time.time())
    today_str = datetime.now().strftime("%Y-%m-%d")
    online_cutoff = now - 900  # last 15 minutes
    week_cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        # 1. Online users (active in page_views or searches in last 15 mins)
        online_row = conn.execute(
            """
            SELECT COUNT(DISTINCT visitor_hash) AS cnt FROM (
                SELECT visitor_hash FROM page_views WHERE ts >= ?
                UNION
                SELECT visitor_hash FROM searches WHERE ts >= ?
            )
            """,
            (online_cutoff, online_cutoff),
        ).fetchone()
        online_now = online_row["cnt"] if online_row else 0

        # 2. Visitors & Views Today / 7d / All time
        pv_today = conn.execute(
            "SELECT COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS uniq FROM page_views WHERE date_str = ?",
            (today_str,),
        ).fetchone()
        pv_week = conn.execute(
            "SELECT COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS uniq FROM page_views WHERE date_str >= ?",
            (week_cutoff,),
        ).fetchone()
        pv_total = conn.execute(
            "SELECT COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS uniq FROM page_views"
        ).fetchone()

        # 3. Searches Today / Photo vs Text / All time
        srch_today = conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN search_type='photo' THEN 1 ELSE 0 END) AS photo, SUM(CASE WHEN search_type='text' THEN 1 ELSE 0 END) AS txt FROM searches WHERE date_str = ?",
            (today_str,),
        ).fetchone()
        srch_total = conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN search_type='photo' THEN 1 ELSE 0 END) AS photo, SUM(CASE WHEN search_type='text' THEN 1 ELSE 0 END) AS txt, AVG(elapsed_sec) AS avg_sec FROM searches"
        ).fetchone()

        # 4. Outbound Clicks by Site
        clicks_total = conn.execute("SELECT COUNT(*) AS cnt FROM outbound_clicks").fetchone()["cnt"]
        clicks_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM outbound_clicks WHERE date_str = ?", (today_str,)
        ).fetchone()["cnt"]
        clicks_by_site_rows = conn.execute(
            "SELECT site_id, COUNT(*) AS cnt FROM outbound_clicks GROUP BY site_id ORDER BY cnt DESC"
        ).fetchall()
        clicks_by_site = {r["site_id"]: r["cnt"] for r in clicks_by_site_rows}

        # 5. Daily Chart Data (last N days)
        daily_series = []
        for i in range(days - 1, -1, -1):
            d_obj = datetime.now() - timedelta(days=i)
            d_str = d_obj.strftime("%Y-%m-%d")
            d_label = d_obj.strftime("%d.%m")
            pv_r = conn.execute(
                "SELECT COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS uniq FROM page_views WHERE date_str = ?",
                (d_str,),
            ).fetchone()
            sr_r = conn.execute(
                "SELECT COUNT(*) AS cnt FROM searches WHERE date_str = ?",
                (d_str,),
            ).fetchone()
            cl_r = conn.execute(
                "SELECT COUNT(*) AS cnt FROM outbound_clicks WHERE date_str = ?",
                (d_str,),
            ).fetchone()
            daily_series.append({
                "date": d_str,
                "label": d_label,
                "views": pv_r["views"] or 0,
                "unique_visitors": pv_r["uniq"] or 0,
                "searches": sr_r["cnt"] or 0,
                "clicks": cl_r["cnt"] or 0,
            })

        # 6. Top Search Queries
        top_queries_rows = conn.execute(
            """
            SELECT LOWER(query) AS q_norm, MAX(query) AS q_display, COUNT(*) AS cnt, MAX(ai_title) AS sample_title
            FROM searches
            WHERE query != ''
            GROUP BY q_norm
            ORDER BY cnt DESC
            LIMIT 12
            """
        ).fetchall()
        top_queries = [
            {
                "query": r["q_display"],
                "count": r["cnt"],
                "ai_title": r["sample_title"] or "",
            }
            for r in top_queries_rows
        ]

        # 7. Recent Searches Feed (last 25)
        recent_searches_rows = conn.execute(
            """
            SELECT ts, search_type, query, ai_title, results_count, elapsed_sec, visitor_hash
            FROM searches
            ORDER BY id DESC
            LIMIT 25
            """
        ).fetchall()
        recent_searches = [
            {
                "time_str": datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M:%S"),
                "search_type": r["search_type"],
                "query": r["query"],
                "ai_title": r["ai_title"] or "",
                "results_count": r["results_count"],
                "elapsed_sec": round(r["elapsed_sec"] or 0.0, 2),
                "visitor": r["visitor_hash"][:6],
            }
            for r in recent_searches_rows
        ]

        # 8. Recent Outbound Clicks (last 15)
        recent_clicks_rows = conn.execute(
            """
            SELECT ts, site_id, model_title, model_url, visitor_hash
            FROM outbound_clicks
            ORDER BY id DESC
            LIMIT 15
            """
        ).fetchall()
        recent_clicks = [
            {
                "time_str": datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M:%S"),
                "site_id": r["site_id"],
                "model_title": r["model_title"] or "3D Model",
                "model_url": r["model_url"] or "#",
                "visitor": r["visitor_hash"][:6],
            }
            for r in recent_clicks_rows
        ]

        # 9. Device Breakdown
        dev_rows = conn.execute(
            "SELECT device, COUNT(DISTINCT visitor_hash) AS cnt FROM page_views GROUP BY device"
        ).fetchall()
        devices = {r["device"] or "💻 ПК": r["cnt"] for r in dev_rows}

        return {
            "online_now": online_now,
            "today": {
                "views": pv_today["views"] or 0,
                "unique_visitors": pv_today["uniq"] or 0,
                "searches": srch_today["total"] or 0,
                "photo_searches": srch_today["photo"] or 0,
                "text_searches": srch_today["txt"] or 0,
                "clicks": clicks_today or 0,
            },
            "week": {
                "views": pv_week["views"] or 0,
                "unique_visitors": pv_week["uniq"] or 0,
            },
            "all_time": {
                "views": pv_total["views"] or 0,
                "unique_visitors": pv_total["uniq"] or 0,
                "searches": srch_total["total"] or 0,
                "photo_searches": srch_total["photo"] or 0,
                "text_searches": srch_total["txt"] or 0,
                "avg_search_sec": round(srch_total["avg_sec"] or 0.0, 2),
                "clicks": clicks_total or 0,
            },
            "clicks_by_site": clicks_by_site,
            "daily_series": daily_series,
            "top_queries": top_queries,
            "recent_searches": recent_searches,
            "recent_clicks": recent_clicks,
            "devices": devices,
        }
    finally:
        conn.close()


init_db()
