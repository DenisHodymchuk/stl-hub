import concurrent.futures
import html
import io
import json
import re
import threading
import time
import urllib.parse
from typing import List, Dict, Any, Optional
from curl_cffi import requests
from bs4 import BeautifulSoup
from PIL import Image

# Thread-local session pool to reuse TCP/TLS connections and avoid repeated handshakes
_thread_local = threading.local()

# Semaphore to prevent concurrent TLS handshake drops on Yahoo Images
_yahoo_semaphore = threading.Semaphore(2)

# In-memory TTL cache for site search queries: (site_name, query_lower, limit) -> (timestamp, results)
_SITE_CACHE: Dict[tuple, tuple] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 900  # 15 minutes

# Cached Thingiverse JWT view token
_TV_TOKEN_LOCK = threading.Lock()
_TV_VIEW_TOKEN: str = ""
_TV_TOKEN_EXPIRES_AT: float = 0.0


def create_session() -> requests.Session:
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session(impersonate="chrome")
        _thread_local.session = sess
    return sess


def _get_cached(site_key: str, query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    key = (site_key, query.strip().lower(), limit)
    with _CACHE_LOCK:
        entry = _SITE_CACHE.get(key)
        if entry and (time.time() - entry[0] < _CACHE_TTL_SECONDS):
            # Return shallow copies of dicts so similarity boosting doesn't mutate cache
            return [dict(item) for item in entry[1]]
    return None


def _set_cached(site_key: str, query: str, limit: int, results: List[Dict[str, Any]]) -> None:
    if not results:
        return
    key = (site_key, query.strip().lower(), limit)
    with _CACHE_LOCK:
        if len(_SITE_CACHE) > 300:
            _SITE_CACHE.clear()
        _SITE_CACHE[key] = (time.time(), [dict(item) for item in results])


def extract_keywords_from_title(title: str) -> str:
    """Extracts a clean 2-3 word functional English query from a MakerWorld model title."""
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", title or "")
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "model", "print",
        "printed", "printable", "stl", "file", "free", "makerworld", "bambu",
        "lab", "a1", "p1p", "p1s", "x1c", "ams", "mini", "version", "v1", "v2",
        "new", "custom", "easy", "no", "support", "supports", "edition", "by"
    }
    words = [w.lower() for w in clean.split() if len(w) >= 3 and w.lower() not in stop and not w.isdigit()]
    return " ".join(words[:3])


def search_makerworld_by_image(image_bytes: bytes, limit: int = 40) -> Dict[str, Any]:
    """
    Uploads the user's photo to MakerWorld's native visual search engine (picsearch)
    and returns up to `limit` visually matched 3D models + their titles/tags.
    """
    results: List[Dict[str, Any]] = []
    visual_hints: List[str] = []
    extracted_queries: List[str] = []
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if max(img.size) > 512:
            img.thumbnail((512, 512), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        jpeg_bytes = buf.getvalue()

        session = create_session()
        r1 = session.post(
            "https://makerworld.com/api/v1/design-user-service/user/upload/picsearch",
            json={"fileName": "search.jpg"},
            headers={
                "X-BBL-Client-Type": "web",
                "X-BBL-Client-Version": "00.00.00.01",
                "X-BBL-App-Source": "makerworld",
                "X-BBL-Client-Name": "MakerWorld",
            },
            timeout=7,
        )
        if r1.status_code != 200:
            return {"results": results, "visual_hints": visual_hints, "extracted_queries": extracted_queries}

        data1 = r1.json()
        upload_url = data1.get("url")
        cdn_prefix = data1.get("cdnPrefix", "https://makerworld.bblmw.com")
        if not upload_url:
            return {"results": results, "visual_hints": visual_hints, "extracted_queries": extracted_queries}

        r2 = session.put(upload_url, data=jpeg_bytes, timeout=7)
        if r2.status_code != 200:
            return {"results": results, "visual_hints": visual_hints, "extracted_queries": extracted_queries}

        parsed_up = urllib.parse.urlparse(upload_url)
        parsed_cdn = urllib.parse.urlparse(cdn_prefix)
        final_img_url = f"{parsed_cdn.scheme}://{parsed_cdn.netloc}{parsed_up.path}"

        batches = [0, 20] if limit > 20 else [0]
        seen_ids = set()
        for offset in batches:
            batch_lim = min(20, limit - len(results))
            if batch_lim <= 0:
                break
            r3 = session.get(
                "https://makerworld.com/api/v1/search-service/select/design2",
                params={"image": final_img_url, "offset": offset, "limit": batch_lim},
                timeout=7,
            )
            if r3.status_code == 200:
                hits = r3.json().get("hits", []) or []
                if not hits:
                    break
                for hit in hits:
                    model_id = hit.get("id")
                    if model_id in seen_ids:
                        continue
                    seen_ids.add(model_id)
                    idx = len(results)
                    slug = hit.get("slug") or ""
                    cover = hit.get("cover") or ""
                    creator = hit.get("designCreator") or {}
                    title_en = hit.get("titleTranslated") or hit.get("title") or "MakerWorld Model"
                    tags = hit.get("tags") or []
                    if len(visual_hints) < 12:
                        visual_hints.append(f"{title_en} (tags: {', '.join(tags[:5])})")
                    if len(extracted_queries) < 4:
                        kw = extract_keywords_from_title(title_en)
                        if kw and kw not in extracted_queries:
                            extracted_queries.append(kw)

                    results.append({
                        "id": f"makerworld_{model_id}",
                        "site": "MakerWorld",
                        "site_id": "makerworld",
                        "title": title_en,
                        "url": f"https://makerworld.com/en/models/{model_id}-{slug}" if slug else f"https://makerworld.com/en/models/{model_id}",
                        "image": cover,
                        "thumbnail": cover,
                        "author": creator.get("name") or "MakerWorld Creator",
                        "likes": hit.get("likeCount") or 0,
                        "downloads": hit.get("downloadCount") or hit.get("printCount") or 0,
                        "price": "Безкоштовно",
                        "is_free": True,
                        "similarity": max(78, 98 - (idx // 2)),
                        "is_visual_match": True,
                    })
    except Exception as e:
        print(f"[MakerWorld Visual Search Error] {e}")

    return {"results": results, "visual_hints": visual_hints, "extracted_queries": extracted_queries}


def search_printables(query: str, limit: int = 24) -> List[Dict[str, Any]]:
    cached = _get_cached("printables", query, limit)
    if cached is not None:
        return cached

    results = []
    try:
        session = create_session()
        url = "https://api.printables.com/graphql/"
        gql = """
        query Search($q: String!, $limit: Int!) {
          searchPrints2(query: $q, limit: $limit) {
            items {
              id
              name
              slug
              image {
                filePath
              }
              user {
                publicUsername
              }
              likesCount
              downloadCount
              price
            }
          }
        }
        """
        resp = session.post(
            url,
            json={"query": gql, "variables": {"q": query, "limit": limit}},
            timeout=6,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data", {}).get("searchPrints2", {}).get("items", []) or []
            for idx, item in enumerate(items):
                model_id = item.get("id")
                slug = item.get("slug") or ""
                file_path = (item.get("image") or {}).get("filePath")
                img_url = f"https://media.printables.com/{file_path}" if file_path else ""
                price_val = item.get("price")
                is_free = price_val is None or float(price_val or 0) == 0
                results.append({
                    "id": f"printables_{model_id}",
                    "site": "Printables",
                    "site_id": "printables",
                    "title": item.get("name") or "3D Model",
                    "url": f"https://www.printables.com/model/{model_id}-{slug}",
                    "image": img_url,
                    "thumbnail": img_url,
                    "author": (item.get("user") or {}).get("publicUsername") or "Printables Creator",
                    "likes": item.get("likesCount") or 0,
                    "downloads": item.get("downloadCount") or 0,
                    "price": "Безкоштовно" if is_free else f"${price_val}",
                    "is_free": is_free,
                    "similarity": max(70, 93 - idx),
                })
            _set_cached("printables", query, limit, results)
    except Exception as e:
        print(f"[Printables Error] {e}")
    return results


def search_makerworld(query: str, limit: int = 24) -> List[Dict[str, Any]]:
    cached = _get_cached("makerworld", query, limit)
    if cached is not None:
        return cached

    results = []
    try:
        session = create_session()
        q_enc = urllib.parse.quote(query)
        url = f"https://makerworld.com/api/v1/search-service/select/design2?keyword={q_enc}&offset=0&limit={limit}"
        resp = session.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            hits = data.get("hits", []) or []
            for idx, hit in enumerate(hits):
                model_id = hit.get("id")
                slug = hit.get("slug") or ""
                cover = hit.get("cover") or ""
                creator = hit.get("designCreator") or {}
                results.append({
                    "id": f"makerworld_{model_id}",
                    "site": "MakerWorld",
                    "site_id": "makerworld",
                    "title": hit.get("titleTranslated") or hit.get("title") or "MakerWorld Model",
                    "url": f"https://makerworld.com/en/models/{model_id}-{slug}" if slug else f"https://makerworld.com/en/models/{model_id}",
                    "image": cover,
                    "thumbnail": cover,
                    "author": creator.get("name") or "MakerWorld Creator",
                    "likes": hit.get("likeCount") or 0,
                    "downloads": hit.get("downloadCount") or hit.get("printCount") or 0,
                    "price": "Безкоштовно",
                    "is_free": True,
                    "similarity": max(70, 93 - idx),
                })
            _set_cached("makerworld", query, limit, results)
    except Exception as e:
        print(f"[MakerWorld Error] {e}")
    return results


def search_thangs(query: str, limit: int = 24) -> List[Dict[str, Any]]:
    cached = _get_cached("thangs", query, limit)
    if cached is not None:
        return cached

    results = []
    try:
        session = create_session()
        q_enc = urllib.parse.quote(query)
        url = f"https://thangs.com/api/models/v3/search-by-text?searchTerm={q_enc}&pageSize={limit}&scope=thangs"
        resp = session.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("results", []) or []
            for idx, item in enumerate(items):
                model_id = item.get("modelId") or item.get("externalId")
                thumbs = item.get("thumbnails") or []
                thumb = item.get("thumbnailUrl") or (thumbs[0] if thumbs else "")
                page_url = item.get("modelPageUrl") or f"https://thangs.com/3d-model/{model_id}"
                if page_url.startswith("/"):
                    page_url = f"https://thangs.com{page_url}"
                market = item.get("marketplaceInfo") or {}
                price_val = market.get("price")
                is_free = not price_val or float(price_val or 0) == 0
                results.append({
                    "id": f"thangs_{model_id}",
                    "site": "Thangs",
                    "site_id": "thangs",
                    "title": item.get("name") or item.get("modelTitle") or "Thangs 3D Model",
                    "url": page_url,
                    "image": thumb,
                    "thumbnail": thumb,
                    "author": item.get("ownerUsername") or "Thangs Designer",
                    "likes": item.get("likesCount") or item.get("nLikes") or 0,
                    "downloads": item.get("downloadCount") or item.get("nDownloads") or 0,
                    "price": "Безкоштовно" if is_free else f"${price_val}",
                    "is_free": is_free,
                    "similarity": max(70, 92 - idx),
                })
            _set_cached("thangs", query, limit, results)
    except Exception as e:
        print(f"[Thangs Error] {e}")
    return results


def search_myminifactory(query: str, limit: int = 24) -> List[Dict[str, Any]]:
    cached = _get_cached("myminifactory", query, limit)
    if cached is not None:
        return cached

    results = []
    try:
        session = create_session()
        q_enc = urllib.parse.quote(query)
        url = f"https://www.myminifactory.com/search/fetch_search?query={q_enc}"
        resp = session.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("objectResults", []) or []
            for idx, item in enumerate(items[:limit]):
                model_id = item.get("id")
                slug = item.get("url") or ""
                abs_url = item.get("absolute_url") or f"https://www.myminifactory.com/object/3d-print-{slug}"
                img = item.get("obj_img") or item.get("obj_original_img") or ""
                price_info = item.get("price") or {}
                val = price_info.get("value", 0)
                sym = price_info.get("symbol", "$")
                try:
                    is_free = float(val or 0) == 0
                except ValueError:
                    is_free = False
                results.append({
                    "id": f"mmf_{model_id}",
                    "site": "MyMiniFactory",
                    "site_id": "myminifactory",
                    "title": item.get("name") or "MyMiniFactory Model",
                    "url": abs_url,
                    "image": img,
                    "thumbnail": img,
                    "author": item.get("user_name") or item.get("user_username") or "MMF Creator",
                    "likes": item.get("visits") or 0,
                    "downloads": 0,
                    "price": "Безкоштовно" if is_free else f"{sym}{val}",
                    "is_free": is_free,
                    "similarity": max(70, 91 - idx),
                })
            _set_cached("myminifactory", query, limit, results)
    except Exception as e:
        print(f"[MyMiniFactory Error] {e}")
    return results


def _fetch_bing_async_images(search_q: str, timeout: int = 6) -> List[Dict[str, str]]:
    """
    Fast & reliable Bing Async Images parser (~0.3s response, no TLS drops).
    Returns list of dicts with keys: purl, murl, turl, t.
    """
    items: List[Dict[str, str]] = []
    try:
        session = create_session()
        resp = session.get(
            "https://www.bing.com/images/async",
            params={"q": search_q, "first": 0, "count": 35, "adlt": "off"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            for m_raw in re.findall(r'm="(\{[^"]+\})"', resp.text):
                try:
                    meta = json.loads(html.unescape(m_raw))
                    items.append(meta)
                except Exception:
                    pass
    except Exception as e:
        print(f"[Bing Async Error] {e}")
    return items


def _fetch_yahoo_images(search_q: str, timeout: int = 6) -> str:
    """Rate-limited Yahoo Images fetcher with automatic retry on SSL connection resets."""
    with _yahoo_semaphore:
        for attempt in range(2):
            try:
                session = create_session() if attempt == 0 else requests.Session(impersonate="chrome")
                resp = session.get(
                    "https://images.search.yahoo.com/search/images",
                    params={"p": search_q},
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    return resp.text
            except Exception as e:
                if attempt == 1:
                    print(f"[Yahoo Images Error] {e}")
                time.sleep(0.15)
    return ""


def _clean_cults_title(raw_title: str, ref_url: str) -> tuple[str, bool]:
    is_free = bool(re.search(r"\bfree\b|\bgratis\b|\bgratuit\b", raw_title, re.I))
    clean = re.sub(r"(?:・|\||•|-)\s*(?:STL|Cults|3D print|Impresión 3D|Modèle 3D|3D model).*$", "", raw_title, flags=re.I).strip()
    clean = re.sub(r"^(?:Free 3D file|STL file|3D file|Archivo STL|Fichier STL|Impresión 3D)\s*", "", clean, flags=re.I).strip()
    if not clean:
        clean = ref_url.rstrip("/").split("/")[-1].replace("-", " ").title()
    return clean, is_free


def search_cults3d(query: str, limit: int = 24) -> List[Dict[str, Any]]:
    cached = _get_cached("cults3d", query, limit)
    if cached is not None:
        return cached

    results: List[Dict[str, Any]] = []
    seen = set()

    # 1. Fast primary search via Bing Async Images (~0.3s, detects Free 3D files!)
    bing_items = _fetch_bing_async_images(f"site:cults3d.com/en/3d-model {query}")
    if len(bing_items) < 8:
        bing_items.extend(_fetch_bing_async_images(f"site:cults3d.com {query}"))

    for meta in bing_items:
        ref_url = meta.get("purl", "")
        if not ref_url or "cults3d.com" not in ref_url or "/3d-model/" not in ref_url:
            continue
        # Normalize language URL to English if possible
        norm_url = re.sub(r"cults3d\.com/(?:fr|es|de|pt|ru)/3d-model/", "cults3d.com/en/3d-model/", ref_url)
        if norm_url in seen:
            continue
        seen.add(norm_url)

        thumb = meta.get("turl") or meta.get("murl") or ""
        orig_img = meta.get("murl") or thumb
        raw_title = meta.get("t") or ""
        clean_title, is_free = _clean_cults_title(raw_title, norm_url)

        results.append({
            "id": f"cults3d_{abs(hash(norm_url)) % 1000000}",
            "site": "Cults3D",
            "site_id": "cults3d",
            "title": clean_title,
            "url": norm_url,
            "image": thumb or orig_img,
            "thumbnail": thumb or orig_img,
            "author": "Cults3D Designer",
            "likes": 0,
            "downloads": 0,
            "price": "Безкоштовно" if is_free else "Див. на сайті",
            "is_free": True if is_free else None,
            "similarity": max(70, 91 - len(results)),
        })
        if len(results) >= limit:
            break

    # 2. Supplement with Yahoo Images if Bing returned fewer than 10 items
    if len(results) < min(10, limit):
        html_text = _fetch_yahoo_images(f"site:cults3d.com {query}")
        if html_text:
            soup = BeautifulSoup(html_text, "html.parser")
            for a in soup.select("a.media-tile[data-referenceurl]"):
                ref_url = a.get("data-referenceurl", "")
                orig_img = a.get("data-origurl", "")
                if not ref_url or "cults3d.com" not in ref_url:
                    continue
                norm_url = re.sub(r"cults3d\.com/(?:fr|es|de|pt|ru)/3d-model/", "cults3d.com/en/3d-model/", ref_url)
                if norm_url in seen:
                    continue
                seen.add(norm_url)
                img_el = a.find("img")
                thumb = (img_el.get("src") or img_el.get("data-src")) if img_el else orig_img
                title_el = a.select_one(".tile-title")
                raw_title = title_el.get_text(" ", strip=True) if title_el else ""
                clean_title, is_free = _clean_cults_title(raw_title, norm_url)
                results.append({
                    "id": f"cults3d_{abs(hash(norm_url)) % 1000000}",
                    "site": "Cults3D",
                    "site_id": "cults3d",
                    "title": clean_title,
                    "url": norm_url,
                    "image": thumb or orig_img,
                    "thumbnail": thumb or orig_img,
                    "author": "Cults3D Designer",
                    "likes": 0,
                    "downloads": 0,
                    "price": "Безкоштовно" if is_free else "Див. на сайті",
                    "is_free": True if is_free else None,
                    "similarity": max(70, 91 - len(results)),
                })
                if len(results) >= limit:
                    break

    _set_cached("cults3d", query, limit, results)
    return results


def _get_thingiverse_token() -> str:
    global _TV_VIEW_TOKEN, _TV_TOKEN_EXPIRES_AT
    now = time.time()
    if _TV_VIEW_TOKEN and now < _TV_TOKEN_EXPIRES_AT:
        return _TV_VIEW_TOKEN
    with _TV_TOKEN_LOCK:
        if _TV_VIEW_TOKEN and time.time() < _TV_TOKEN_EXPIRES_AT:
            return _TV_VIEW_TOKEN
        try:
            session = create_session()
            rv = session.get("https://api.thingiverse.com/v2/auth/view", timeout=5)
            if rv.status_code == 200:
                token = rv.json().get("access", "")
                if token:
                    _TV_VIEW_TOKEN = token
                    _TV_TOKEN_EXPIRES_AT = time.time() + 3600 * 6  # valid for 24h, refresh every 6h
                    return _TV_VIEW_TOKEN
        except Exception as e:
            print(f"[Thingiverse Auth Error] {e}")
    return ""


def search_thingiverse(query: str, limit: int = 24) -> List[Dict[str, Any]]:
    cached = _get_cached("thingiverse", query, limit)
    if cached is not None:
        return cached

    results: List[Dict[str, Any]] = []
    seen = set()

    # 1. Native Thingiverse v2 API (/v2/search/things) — fast, exact, with real likes & authors!
    try:
        token = _get_thingiverse_token()
        if token:
            session = create_session()
            rs = session.get(
                "https://api.thingiverse.com/v2/search/things",
                params={"page": 1, "per_page": limit, "sort": "relevant", "term": query},
                headers={"Authorization": f"Bearer {token}"},
                timeout=6,
            )
            if rs.status_code == 401:
                global _TV_TOKEN_EXPIRES_AT
                _TV_TOKEN_EXPIRES_AT = 0.0
                token = _get_thingiverse_token()
                if token:
                    rs = session.get(
                        "https://api.thingiverse.com/v2/search/things",
                        params={"page": 1, "per_page": limit, "sort": "relevant", "term": query},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=6,
                    )
            if rs.status_code == 200:
                hits = rs.json().get("hits", []) or []
                for idx, hit in enumerate(hits[:limit]):
                    thing_id = hit.get("id")
                    pub_url = hit.get("public_url") or f"https://www.thingiverse.com/thing:{thing_id}"
                    if pub_url in seen:
                        continue
                    seen.add(pub_url)
                    thumb = hit.get("preview_image") or hit.get("thumbnail") or ""
                    creator = hit.get("creator") or {}
                    results.append({
                        "id": f"thingiverse_{thing_id}",
                        "site": "Thingiverse",
                        "site_id": "thingiverse",
                        "title": hit.get("name") or f"Thingiverse #{thing_id}",
                        "url": pub_url,
                        "image": thumb,
                        "thumbnail": thumb,
                        "author": creator.get("name") or "Thingiverse Creator",
                        "likes": hit.get("like_count") or 0,
                        "downloads": hit.get("collect_count") or 0,
                        "price": "Безкоштовно",
                        "is_free": True,
                        "similarity": max(70, 92 - idx),
                    })
    except Exception as e:
        print(f"[Thingiverse v2 API Error] {e}")

    # 2. Fallback to Bing Async Images if v2 API returned nothing
    if not results:
        bing_items = _fetch_bing_async_images(f'"thingiverse.com/thing:" {query}')
        for meta in bing_items:
            ref_url = meta.get("purl", "")
            if not ref_url or "thingiverse.com/thing:" not in ref_url:
                continue
            base_thing_match = re.search(r"(https?://www\.thingiverse\.com/thing:\d+)", ref_url)
            clean_url = base_thing_match.group(1) if base_thing_match else ref_url
            if clean_url in seen:
                continue
            seen.add(clean_url)
            thumb = meta.get("turl") or meta.get("murl") or ""
            orig_img = meta.get("murl") or thumb
            raw_title = meta.get("t") or ""
            clean_title = re.sub(r"\s*(?:-|•|\|)\s*Thingiverse.*$", "", raw_title, flags=re.I).strip()
            results.append({
                "id": f"thingiverse_{clean_url.split(':')[-1]}",
                "site": "Thingiverse",
                "site_id": "thingiverse",
                "title": clean_title or f"Thingiverse #{clean_url.split(':')[-1]}",
                "url": clean_url,
                "image": thumb or orig_img,
                "thumbnail": thumb or orig_img,
                "author": "Thingiverse Creator",
                "likes": 0,
                "downloads": 0,
                "price": "Безкоштовно",
                "is_free": True,
                "similarity": max(70, 91 - len(results)),
            })
            if len(results) >= limit:
                break

    _set_cached("thingiverse", query, limit, results)
    return results


def search_multi_queries_on_site(search_fn, queries: List[str], limit_total: int = 30) -> List[Dict[str, Any]]:
    """Runs up to 3 distinct search queries in parallel for a site and merges unique results."""
    unique_queries = []
    for q in queries:
        q_clean = q.strip()
        if q_clean and q_clean.lower() not in [x.lower() for x in unique_queries]:
            unique_queries.append(q_clean)
        if len(unique_queries) >= 3:
            break

    if not unique_queries:
        return []
    if len(unique_queries) == 1:
        return search_fn(unique_queries[0], limit_total)

    per_q = max(12, (limit_total + len(unique_queries) - 1) // len(unique_queries))
    merged: List[Dict[str, Any]] = []
    seen_ids = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(search_fn, q, per_q) for q in unique_queries]
        for f in futs:
            try:
                for item in f.result():
                    uid = item.get("id") or item.get("url")
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        merged.append(item)
            except Exception:
                pass
    return merged[:limit_total]


def _apply_smart_keyword_boost(items: List[Dict[str, Any]], queries: List[str]) -> None:
    """
    Boosts similarity score for models whose title matches multiple core keywords
    from the visual & AI functional queries.
    """
    keywords = set()
    stop_words = {"the", "and", "for", "with", "stl", "3d", "model", "print", "free", "file"}
    for q in queries:
        for w in re.findall(r"[a-zA-Z0-9]{3,}", q.lower()):
            if w not in stop_words:
                keywords.add(w)

    if not keywords:
        return

    primary_words = set(re.findall(r"[a-zA-Z0-9]{3,}", (queries[0] if queries else "").lower())) - stop_words

    for item in items:
        if item.get("is_visual_match"):
            continue
        title_lower = (item.get("title") or "").lower()
        title_words = set(re.findall(r"[a-zA-Z0-9]{3,}", title_lower))
        matched_primary = len(title_words & primary_words)
        matched_any = len(title_words & keywords)

        bonus = 0
        if matched_primary >= 2:
            bonus += 4
        if matched_any >= 3:
            bonus += 3
        elif matched_any == 0:
            bonus -= 4

        base_sim = item.get("similarity", 80)
        item["similarity"] = max(65, min(96, base_sim + bonus))


def search_all_sites(
    query: str,
    limit_per_site: int = 30,
    user_image_bytes: Optional[bytes] = None,
    secondary_queries: Optional[List[str]] = None,
    precomputed_makerworld_visual: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    other_sites = {
        "printables": search_printables,
        "thangs": search_thangs,
        "myminifactory": search_myminifactory,
        "cults3d": search_cults3d,
        "thingiverse": search_thingiverse,
    }

    queries_to_use = [query]
    if secondary_queries:
        for sq in secondary_queries:
            if sq and sq.lower() not in [x.lower() for x in queries_to_use]:
                queries_to_use.append(sq)
            if len(queries_to_use) >= 3:
                break

    all_items: List[Dict[str, Any]] = []
    counts_by_site: Dict[str, int] = {
        "printables": 0,
        "makerworld": 0,
        "thangs": 0,
        "myminifactory": 0,
        "cults3d": 0,
        "thingiverse": 0,
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        future_to_site = {
            executor.submit(search_multi_queries_on_site, fn, queries_to_use, limit_per_site): site_key
            for site_key, fn in other_sites.items()
        }

        if precomputed_makerworld_visual is not None and len(precomputed_makerworld_visual) > 0:
            mw_text_fut = executor.submit(search_makerworld, query, 16)
            mw_items = [dict(x) for x in precomputed_makerworld_visual]
            seen_mw = {x["id"] for x in mw_items}
            try:
                for extra in mw_text_fut.result():
                    if extra["id"] not in seen_mw:
                        seen_mw.add(extra["id"])
                        mw_items.append(dict(extra))
            except Exception:
                pass
            counts_by_site["makerworld"] = len(mw_items)
            all_items.extend(mw_items)
        else:
            future_to_site[executor.submit(search_multi_queries_on_site, search_makerworld, queries_to_use, limit_per_site)] = "makerworld"

        for future in concurrent.futures.as_completed(future_to_site):
            site_key = future_to_site[future]
            try:
                site_results = future.result()
                counts_by_site[site_key] = len(site_results)
                all_items.extend(site_results)
            except Exception as e:
                print(f"[Search All Error - {site_key}] {e}")
                counts_by_site[site_key] = 0

    _apply_smart_keyword_boost(all_items, queries_to_use)
    all_items.sort(key=lambda x: (x.get("similarity", 0), x.get("likes", 0)), reverse=True)

    return {
        "query": query,
        "total": len(all_items),
        "counts_by_site": counts_by_site,
        "results": all_items,
    }
