from collections import OrderedDict
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import time
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Form, Query, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_vision import analyze_image_with_gemini, load_config, save_config, CONFIG_PATH
from analytics import log_page_view, log_search_event, log_click_event, get_dashboard_stats, log_feedback
from scrapers import search_all_sites, search_makerworld_by_image, create_session, _SEARCH_EXECUTOR

app = FastAPI(title="STL-HUB — Візуальний пошук 3D моделей по фото на 6 сайтах")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

MAX_IMAGE_CACHE_ITEMS = 150
MAX_RESULT_CACHE_ITEMS = 300

IMAGE_CACHE: OrderedDict[str, bytes] = OrderedDict()
VISUAL_MW_CACHE: OrderedDict[str, dict] = OrderedDict()
AI_ANALYSIS_CACHE: OrderedDict[str, dict] = OrderedDict()


def _cache_put(cache: OrderedDict, key: str, value: object, max_items: int) -> None:
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > max_items:
        cache.popitem(last=False)


class SettingsPayload(BaseModel):
    gemini_api_key: str
    model: Optional[str] = "gemini-2.5-flash"


class AdminLoginPayload(BaseModel):
    password: str


class AdminPasswordChangePayload(BaseModel):
    current_password: str
    new_password: str


class ClickTrackPayload(BaseModel):
    site_id: str
    title: Optional[str] = ""
    url: Optional[str] = ""


class FeedbackPayload(BaseModel):
    message: str
    contact: Optional[str] = ""


def _get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "") or "127.0.0.1"


def _get_admin_password() -> str:
    cfg = load_config()
    return cfg.get("admin_password") or os.environ.get("ADMIN_PASSWORD") or "stlhub2026"


def _make_admin_token(password: str) -> str:
    return hmac.new(b"stlhub_admin_secret_key", password.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_admin_token(token: Optional[str]) -> bool:
    if not token:
        return False
    expected = _make_admin_token(_get_admin_password())
    return hmac.compare_digest(token.strip(), expected)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    _SEARCH_EXECUTOR.submit(
        log_page_view,
        _get_client_ip(request),
        request.headers.get("user-agent", ""),
        request.headers.get("referer", ""),
    )
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    admin_path = os.path.join(STATIC_DIR, "admin.html")
    with open(admin_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    fav_path = os.path.join(STATIC_DIR, "favicon.svg")
    with open(fav_path, "rb") as f:
        return Response(
            content=f.read(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )


@app.get("/api/settings")
async def get_settings():
    cfg = load_config()
    key = cfg.get("gemini_api_key", "")
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("Встановлено" if key else "")
    return {
        "has_key": bool(key),
        "masked_key": masked,
        "model": cfg.get("model", "gemini-2.5-flash"),
    }


@app.post("/api/settings")
async def update_settings(payload: SettingsPayload):
    if not payload.gemini_api_key.strip():
        raise HTTPException(status_code=400, detail="API ключ не може бути порожнім")
    save_config(payload.gemini_api_key, payload.model or "gemini-2.5-flash")
    return {"status": "ok"}


@app.post("/api/search-by-photo")
async def search_by_photo(request: Request):
    t_start = time.perf_counter()
    image_bytes: Optional[bytes] = None
    image_url: Optional[str] = None
    hint: str = ""
    limit_per_site: int = 30
    fast_mode: bool = False

    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body = await request.json()
        b64_str = body.get("image_base64") or ""
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]
        if b64_str:
            image_bytes = base64.b64decode(b64_str)
        image_url = body.get("image_url") or ""
        hint = body.get("hint") or ""
        limit_per_site = int(body.get("limit_per_site") or 30)
        fast_mode = bool(body.get("fast_mode", False))
    else:
        form = await request.form()
        file_obj = form.get("file")
        if file_obj is not None and hasattr(file_obj, "read"):
            image_bytes = await file_obj.read()
        image_url = str(form.get("image_url") or "")
        hint = str(form.get("hint") or "")
        limit_per_site = int(form.get("limit_per_site") or 30)
        fast_mode = str(form.get("fast_mode") or "").lower() in ("true", "1", "yes")

    if image_bytes is None and image_url and image_url.strip():
        try:
            sess = create_session()
            r = sess.get(image_url.strip(), timeout=8)
            if r.status_code == 200:
                image_bytes = r.content
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Не вдалося завантажити фото за посиланням: {e}")

    if not image_bytes:
        raise HTTPException(status_code=400, detail="Будь ласка, завантажте фото або вставте посилання на зображення.")

    img_hash = hashlib.md5(image_bytes).hexdigest()[:12]
    _cache_put(IMAGE_CACHE, img_hash, image_bytes, MAX_IMAGE_CACHE_ITEMS)
    cache_key = f"{img_hash}:{(hint or '').strip().lower()}"

    # Step 1 & 2 IN PARALLEL: MakerWorld Native Visual Search + Gemini 2.5 Flash Vision
    if img_hash in VISUAL_MW_CACHE:
        mw_fut = None
        VISUAL_MW_CACHE.move_to_end(img_hash)
        mw_visual = VISUAL_MW_CACHE[img_hash]
    else:
        mw_fut = _SEARCH_EXECUTOR.submit(search_makerworld_by_image, image_bytes, 50)

    if cache_key in AI_ANALYSIS_CACHE:
        ai_fut = None
        AI_ANALYSIS_CACHE.move_to_end(cache_key)
        ai_info = AI_ANALYSIS_CACHE[cache_key]
    else:
        ai_fut = _SEARCH_EXECUTOR.submit(analyze_image_with_gemini, image_bytes, hint or "", None)

    if mw_fut is not None:
        mw_visual = mw_fut.result()
        if mw_visual.get("results"):
            _cache_put(VISUAL_MW_CACHE, img_hash, mw_visual, MAX_RESULT_CACHE_ITEMS)

    mw_visual_results = mw_visual.get("results", [])
    mw_extracted_queries = mw_visual.get("extracted_queries", [])

    if ai_fut is not None:
        try:
            ai_info = ai_fut.result()
            _cache_put(AI_ANALYSIS_CACHE, cache_key, ai_info, MAX_RESULT_CACHE_ITEMS)
        except Exception as e:
            # Fallback if Gemini fails but MakerWorld visual search succeeded
            if mw_extracted_queries:
                fallback_q = mw_extracted_queries[0]
                ai_info = {
                    "title_uk": hint or f"3D-модель ({fallback_q})",
                    "category_uk": "Візуальний збіг MakerWorld",
                    "description_uk": "Підібрано за візуальною схожістю на MakerWorld.",
                    "primary_query": fallback_q,
                    "alternative_queries": mw_extracted_queries,
                    "detected_features": ["Візуальний пошук"],
                }
            else:
                raise HTTPException(status_code=500, detail=f"Помилка ШІ-аналізу фото: {e}")

    # Combine Gemini functional queries with MakerWorld's top visual match keywords!
    primary_query = ai_info["primary_query"]
    alt_queries = list(ai_info.get("alternative_queries", []))
    for mw_q in mw_extracted_queries:
        if mw_q and mw_q.lower() not in [q.lower() for q in alt_queries]:
            alt_queries.append(mw_q)
    ai_info["alternative_queries"] = alt_queries[:6]

    secondary_candidates = [q for q in alt_queries if q.lower() != primary_query.lower()]
    # Ensure at least one MakerWorld visual keyword query is included in secondary_queries if available
    if mw_extracted_queries and mw_extracted_queries[0].lower() != primary_query.lower():
        mw_top_q = mw_extracted_queries[0]
        secondary_candidates = [mw_top_q] + [q for q in secondary_candidates if q.lower() != mw_top_q.lower()]

    if fast_mode:
        # Return Phase 1 immediately (AI analysis + MakerWorld visual matches) so the UI renders in ~1.5s!
        elapsed_sec = round(time.perf_counter() - t_start, 2)
        _SEARCH_EXECUTOR.submit(
            log_search_event,
            _get_client_ip(request),
            request.headers.get("user-agent", ""),
            "photo",
            primary_query,
            ai_info.get("title_uk", ""),
            len(mw_visual_results),
            elapsed_sec,
        )
        return {
            "image_id": img_hash,
            "ai_analysis": ai_info,
            "secondary_queries": secondary_candidates[:2],
            "search": {
                "query": primary_query,
                "total": len(mw_visual_results),
                "counts_by_site": {
                    "printables": 0,
                    "makerworld": len(mw_visual_results),
                    "thangs": 0,
                    "myminifactory": 0,
                    "cults3d": 0,
                    "thingiverse": 0,
                },
                "results": mw_visual_results,
                "elapsed_seconds": elapsed_sec,
            },
        }

    # Step 3: Search the other 5 sites in parallel + merge with MakerWorld's visual matches
    search_data = search_all_sites(
        query=primary_query,
        limit_per_site=limit_per_site,
        user_image_bytes=image_bytes,
        secondary_queries=secondary_candidates[:2],
        precomputed_makerworld_visual=mw_visual_results,
    )
    elapsed_sec = round(time.perf_counter() - t_start, 2)
    search_data["elapsed_seconds"] = elapsed_sec

    _SEARCH_EXECUTOR.submit(
        log_search_event,
        _get_client_ip(request),
        request.headers.get("user-agent", ""),
        "photo",
        primary_query,
        ai_info.get("title_uk", ""),
        search_data.get("total", 0),
        elapsed_sec,
    )

    return {
        "image_id": img_hash,
        "ai_analysis": ai_info,
        "search": search_data,
    }


@app.get("/api/search")
async def search_by_text(
    request: Request,
    q: str = Query(..., min_length=1),
    secondary_q: Optional[str] = Query(None),
    image_id: Optional[str] = Query(None),
    limit_per_site: int = Query(50, ge=1, le=80),
):
    t_start = time.perf_counter()
    user_img = IMAGE_CACHE.get(image_id) if image_id else None
    precomputed_mw = VISUAL_MW_CACHE.get(image_id, {}).get("results") if image_id and image_id in VISUAL_MW_CACHE else None
    sec_list = [x.strip() for x in (secondary_q or "").split("|") if x.strip()] or None
    search_data = search_all_sites(
        query=q.strip(),
        limit_per_site=limit_per_site,
        user_image_bytes=user_img,
        secondary_queries=sec_list,
        precomputed_makerworld_visual=precomputed_mw,
    )
    elapsed = round(time.perf_counter() - t_start, 2)
    search_data["elapsed_seconds"] = elapsed

    # Only log as a standalone text search if it's not the background Stage 2 fetch of a photo search
    if not secondary_q:
        _SEARCH_EXECUTOR.submit(
            log_search_event,
            _get_client_ip(request),
            request.headers.get("user-agent", ""),
            "text",
            q.strip(),
            "",
            search_data.get("total", 0),
            elapsed,
        )
    return {"search": search_data}


@app.post("/api/track-click")
async def track_click(request: Request, payload: ClickTrackPayload):
    _SEARCH_EXECUTOR.submit(
        log_click_event,
        _get_client_ip(request),
        request.headers.get("user-agent", ""),
        payload.site_id,
        payload.title or "",
        payload.url or "",
    )
    return {"status": "ok"}


@app.post("/api/feedback")
async def submit_feedback(request: Request, payload: FeedbackPayload):
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Повідомлення не може бути порожнім")
    if len(msg) > 2000:
        raise HTTPException(status_code=400, detail="Повідомлення занадто довге (максимум 2000 символів)")
    contact = (payload.contact or "").strip()[:100]
    client_ip = _get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    _SEARCH_EXECUTOR.submit(log_feedback, client_ip, ua, msg, contact)
    return {"status": "ok", "message": "Дякуємо! Ваше повідомлення успішно надіслано."}


@app.post("/api/admin/login")
async def admin_login(payload: AdminLoginPayload):
    if payload.password.strip() != _get_admin_password():
        raise HTTPException(status_code=401, detail="Невірний пароль адміністратора")
    return {"token": _make_admin_token(_get_admin_password())}


@app.get("/api/admin/stats")
async def admin_stats(
    days: int = Query(14, ge=7, le=60),
    x_admin_token: Optional[str] = Header(None),
):
    if not _verify_admin_token(x_admin_token):
        raise HTTPException(status_code=401, detail="Необхідна авторизація")
    return get_dashboard_stats(days=days)


@app.post("/api/admin/change-password")
async def admin_change_password(
    payload: AdminPasswordChangePayload,
    x_admin_token: Optional[str] = Header(None),
):
    if not _verify_admin_token(x_admin_token):
        raise HTTPException(status_code=401, detail="Необхідна авторизація")
    if payload.current_password.strip() != _get_admin_password():
        raise HTTPException(status_code=400, detail="Поточний пароль невірний")
    new_pw = payload.new_password.strip()
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Новий пароль має містити щонайменше 4 символи")
    cfg = load_config()
    cfg["admin_password"] = new_pw
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return {"status": "ok", "token": _make_admin_token(new_pw)}


@app.get("/api/proxy-image")
async def proxy_image(url: str = Query(...)):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL")
    try:
        sess = create_session()
        r = sess.get(url, timeout=6)
        content_type = r.headers.get("content-type", "image/jpeg")
        return Response(
            content=r.content,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Image not found")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
