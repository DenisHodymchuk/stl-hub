import concurrent.futures
import hashlib
import os
import time
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_vision import analyze_image_with_gemini, load_config, save_config
from scrapers import search_all_sites, search_makerworld_by_image, create_session

app = FastAPI(title="STL-HUB — Візуальний пошук 3D моделей по фото на 6 сайтах")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

IMAGE_CACHE: dict[str, bytes] = {}
VISUAL_MW_CACHE: dict[str, dict] = {}
AI_ANALYSIS_CACHE: dict[str, dict] = {}


class SettingsPayload(BaseModel):
    gemini_api_key: str
    model: Optional[str] = "gemini-2.5-flash"


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()


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
async def search_by_photo(
    file: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
    hint: Optional[str] = Form(""),
    limit_per_site: int = Form(30),
):
    t_start = time.perf_counter()
    image_bytes: Optional[bytes] = None

    if file is not None:
        image_bytes = await file.read()
    elif image_url and image_url.strip():
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
    IMAGE_CACHE[img_hash] = image_bytes
    cache_key = f"{img_hash}:{(hint or '').strip().lower()}"

    # Step 1 & 2 IN PARALLEL: MakerWorld Native Visual Search + Gemini 2.5 Flash Vision
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        if img_hash in VISUAL_MW_CACHE:
            mw_fut = None
            mw_visual = VISUAL_MW_CACHE[img_hash]
        else:
            mw_fut = pool.submit(search_makerworld_by_image, image_bytes, 40)

        if cache_key in AI_ANALYSIS_CACHE:
            ai_fut = None
            ai_info = AI_ANALYSIS_CACHE[cache_key]
        else:
            ai_fut = pool.submit(analyze_image_with_gemini, image_bytes, hint or "", None)

        if mw_fut is not None:
            mw_visual = mw_fut.result()
            VISUAL_MW_CACHE[img_hash] = mw_visual

        mw_visual_results = mw_visual.get("results", [])
        mw_extracted_queries = mw_visual.get("extracted_queries", [])

        if ai_fut is not None:
            try:
                ai_info = ai_fut.result()
                AI_ANALYSIS_CACHE[cache_key] = ai_info
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

    return {
        "image_id": img_hash,
        "ai_analysis": ai_info,
        "search": search_data,
    }


@app.get("/api/search")
async def search_by_text(
    q: str = Query(..., min_length=1),
    image_id: Optional[str] = Query(None),
    limit_per_site: int = Query(30, ge=1, le=60),
):
    t_start = time.perf_counter()
    user_img = IMAGE_CACHE.get(image_id) if image_id else None
    search_data = search_all_sites(
        query=q.strip(),
        limit_per_site=limit_per_site,
        user_image_bytes=user_img,
    )
    search_data["elapsed_seconds"] = round(time.perf_counter() - t_start, 2)
    return {"search": search_data}


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
