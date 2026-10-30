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

# Global reusable worker pool for parallel site queries (sized to 8 for CloudLinux NPROC safety)
_SEARCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)

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


_MW_HEADERS = {
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
    "X-BBL-Client-Name": "MakerWorld",
    "Referer": "https://makerworld.com/en",
    "Origin": "https://makerworld.com",
}


def _classify_makerworld_license(raw_lic: Optional[str]) -> Dict[str, Any]:
    """
    Classifies a MakerWorld license string into:
    - commercial_allowed: True (allowed), False (prohibited), None (unknown)
    - license_code: short badge label (e.g. 'CC BY', 'CC BY-NC', 'Standard')
    - license_label: human-readable Ukrainian explanation
    """
    lic = (raw_lic or "").strip()
    lic_upper = lic.upper()
    if not lic:
        return {"commercial_allowed": None, "license_code": "Див. ліцензію", "license_label": "Перевірте умови ліцензії на сайті"}
    if lic_upper in ("CC0", "PUBLIC DOMAIN"):
        return {"commercial_allowed": True, "license_code": "CC0", "license_label": "CC0 Public Domain — комерційний продаж друку дозволено"}
    if lic_upper in ("BY", "CC BY", "CC-BY"):
        return {"commercial_allowed": True, "license_code": "CC BY", "license_label": "CC BY — комерційний продаж друку дозволено (із зазначенням автора)"}
    if lic_upper in ("BY-SA", "CC BY-SA", "CC-BY-SA"):
        return {"commercial_allowed": True, "license_code": "CC BY-SA", "license_label": "CC BY-SA — комерційний продаж друку дозволено (із зазначенням автора)"}
    if lic_upper in ("BY-ND", "CC BY-ND", "CC-BY-ND"):
        return {"commercial_allowed": True, "license_code": "CC BY-ND", "license_label": "CC BY-ND — комерційний продаж оригінальної моделі дозволено (без модифікацій)"}
    if "NC" in lic_upper or "NONCOMMERCIAL" in lic_upper:
        return {"commercial_allowed": False, "license_code": f"CC {lic_upper}", "license_label": f"{lic} — комерційний продаж заборонено (Non-Commercial)"}
    if "EXCLUSIVE" in lic_upper:
        return {"commercial_allowed": False, "license_code": "MW Exclusive", "license_label": "MakerWorld Exclusive — тільки особисте використання (або потрібна підписка автора)"}
    if "STANDARD" in lic_upper:
        return {"commercial_allowed": False, "license_code": "Standard License", "license_label": "Standard Digital File License — тільки для особистого некомерційного використання"}
    return {"commercial_allowed": None, "license_code": lic[:18], "license_label": f"Ліцензія: {lic}"}


def _classify_printables_license(lic_obj: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Classifies a Printables GraphQL license object {id, name} into commercial status.
    """
    if not lic_obj:
        return {"commercial_allowed": None, "license_code": "Див. ліцензію", "license_label": "Перевірте умови ліцензії на сайті"}
    lic_id = str(lic_obj.get("id") or "")
    lic_name = (lic_obj.get("name") or "").strip()
    name_lower = lic_name.lower()

    # Printables explicit commercial / open licenses
    commercial_true_Map = {
        "7": ("CC0", "CC0 Public Domain — комерційний продаж друку дозволено"),
        "1": ("CC BY", "CC BY — комерційний продаж друку дозволено (із зазначенням автора)"),
        "2": ("CC BY-SA", "CC BY-SA — комерційний продаж друку дозволено (із зазначенням автора)"),
        "8": ("CC BY-ND", "CC BY-ND — комерційний продаж друку без модифікацій дозволено"),
        "9": ("GPL v2", "GNU GPL v2.0 — комерційний продаж дозволено"),
        "12": ("GPL v3", "GNU GPL v3.0 — комерційний продаж дозволено"),
        "10": ("LGPL", "GNU LGPL — комерційний продаж дозволено"),
        "11": ("BSD", "BSD License — комерційний продаж дозволено"),
        "14": ("Commercial ND", "Commercial Use (No Derivative) — комерційний продаж дозволено"),
        "15": ("Commercial Use", "Commercial Use — комерційний продаж дозволено"),
        "16": ("OCL v1", "Open Community License — комерційний продаж дозволено"),
        "17": ("OCL v1.1", "Open Community License v1.1 — комерційний продаж дозволено"),
        "18": ("CERN OHL", "CERN Open Hardware Licence — комерційний продаж дозволено"),
        "19": ("OCL + Attr", "Open Community License (Attribution) — комерційний продаж дозволено"),
        "24": ("Apache 2.0", "Apache License 2.0 — комерційний продаж дозволено"),
    }
    if lic_id in commercial_true_Map:
        code, label = commercial_true_Map[lic_id]
        return {"commercial_allowed": True, "license_code": code, "license_label": label}

    commercial_false_Map = {
        "3": ("CC BY-NC", "CC BY-NC — комерційний продаж заборонено (Non-Commercial)"),
        "4": ("CC BY-NC-SA", "CC BY-NC-SA — комерційний продаж заборонено (Non-Commercial)"),
        "6": ("CC BY-NC-ND", "CC BY-NC-ND — комерційний продаж заборонено (Non-Commercial)"),
        "13": ("Standard License", "Standard Digital File License — тільки для особистого використання"),
        "20": ("OCL R&D Only", "Open Community License (R&D) — комерційний продаж обмежено"),
        "21": ("OCL MicroBiz", "Open Community License (Micro Business) — обмежено для мікробізнесу"),
        "22": ("OCL MicroBiz", "Open Community License (Micro Business) — обмежено для мікробізнесу"),
        "23": ("OCL R&D Only", "Open Community License (R&D) — комерційний продаж обмежено"),
    }
    if lic_id in commercial_false_Map:
        code, label = commercial_false_Map[lic_id]
        return {"commercial_allowed": False, "license_code": code, "license_label": label}

    if "noncommercial" in name_lower or "standard digital" in name_lower:
        return {"commercial_allowed": False, "license_code": "Non-Commercial", "license_label": f"{lic_name} — комерційний продаж заборонено"}
    if "commercial use" in name_lower or "public domain" in name_lower:
        return {"commercial_allowed": True, "license_code": "Commercial OK", "license_label": f"{lic_name} — комерційний продаж дозволено"}

    return {"commercial_allowed": None, "license_code": lic_name[:18] or "Див. ліцензію", "license_label": lic_name or "Перевірте ліцензію на сайті"}


def search_makerworld_by_image(image_bytes: bytes, limit: int = 50) -> Dict[str, Any]:
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
            headers=_MW_HEADERS,
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

        batches = [0, 20, 40] if limit > 40 else ([0, 20] if limit > 20 else [0])
        seen_ids = set()

        def _fetch_mw_batch(offset: int):
            sess = create_session()
            return sess.get(
                "https://makerworld.com/api/v1/search-service/select/design2",
                params={"image": final_img_url, "offset": offset, "limit": 20},
                headers=_MW_HEADERS,
                timeout=6,
            )

        batch_futs = [_SEARCH_EXECUTOR.submit(_fetch_mw_batch, off) for off in batches]
        for fut in batch_futs:
            try:
                r3 = fut.result()
            except Exception:
                continue
            if r3.status_code == 200:
                hits = r3.json().get("hits", []) or []
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

                    lic_info = _classify_makerworld_license(hit.get("license"))
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
                        "commercial_allowed": lic_info["commercial_allowed"],
                        "license_code": lic_info["license_code"],
                        "license_label": lic_info["license_label"],
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
              license {
                id
                name
              }
              weight
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
                lic_info = _classify_printables_license(item.get("license"))
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
                    "commercial_allowed": lic_info["commercial_allowed"],
                    "license_code": lic_info["license_code"],
                    "license_label": lic_info["license_label"],
                    "similarity": max(70, 93 - idx),
                    "weight_g": float(item.get("weight") or 0) if item.get("weight") else None,
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
        resp = session.get(url, headers=_MW_HEADERS, timeout=6)
        if resp.status_code != 200:
            session = requests.Session(impersonate="safari")
            resp = session.get(url, headers=_MW_HEADERS, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            hits = data.get("hits", []) or []
            for idx, hit in enumerate(hits):
                model_id = hit.get("id")
                slug = hit.get("slug") or ""
                cover = hit.get("cover") or ""
                creator = hit.get("designCreator") or {}
                lic_info = _classify_makerworld_license(hit.get("license"))
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
                    "commercial_allowed": lic_info["commercial_allowed"],
                    "license_code": lic_info["license_code"],
                    "license_label": lic_info["license_label"],
                    "similarity": max(70, 93 - idx),
                    "weight_g": float(hit.get("weight") or 0) if hit.get("weight") else None,
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
                price_val = market.get("price") or market.get("priceInUSD")
                is_free = not price_val or float(price_val or 0) == 0
                has_plans = bool(item.get("plans"))
                if not is_free or has_plans:
                    th_comm = False
                    th_code = "Потрібна підписка" if has_plans else "Personal Only"
                    th_label = "Модель платна або потребує комерційної підписки автора на Thangs"
                else:
                    th_comm = None
                    th_code = "Див. ліцензію"
                    th_label = "Перевірте ліцензію на сторінці моделі Thangs"
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
                    "commercial_allowed": th_comm,
                    "license_code": th_code,
                    "license_label": th_label,
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
    seen_ids = set()
    try:
        q_enc = urllib.parse.quote(query)
        pages = [1, 2] if limit > 20 else [1]
        for p in pages:
            if len(results) >= limit:
                break
            session = create_session()
            url = f"https://www.myminifactory.com/search/fetch_search?query={q_enc}&page={p}"
            resp = session.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("objectResults", []) or []
                if not items:
                    break
                for item in items:
                    model_id = item.get("id")
                    if model_id in seen_ids:
                        continue
                    seen_ids.add(model_id)
                    idx = len(results)
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
                    if not is_free:
                        mmf_comm = False
                        mmf_code = "Personal Only"
                        mmf_label = "Платна модель MyMiniFactory — стандартна ліцензія лише для особистого друку"
                    else:
                        mmf_comm = None
                        mmf_code = "Див. ліцензію"
                        mmf_label = "Перевірте умови ліцензії на сторінці MyMiniFactory"
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
                        "commercial_allowed": mmf_comm,
                        "license_code": mmf_code,
                        "license_label": mmf_label,
                        "similarity": max(70, 91 - idx),
                    })
                    if len(results) >= limit:
                        break
        if results:
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
            "commercial_allowed": None,
            "license_code": "Див. ліцензію",
            "license_label": "Перевірте тип ліцензії (Cults Private Use / CC) на сторінці моделі",
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
                    "commercial_allowed": None,
                    "license_code": "Див. ліцензію",
                    "license_label": "Перевірте тип ліцензії (Cults Private Use / CC) на сторінці моделі",
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

    # 1. Native Thingiverse v2 API (/v2/search/things) — fast, exact, with real likes, authors & CC license tagging!
    try:
        token = _get_thingiverse_token()
        if token:
            def _fetch_tv_slice(lic_filter: Optional[str]):
                sess = create_session()
                params = {"page": 1, "per_page": limit, "sort": "relevant", "term": query}
                if lic_filter:
                    params["license"] = lic_filter
                r = sess.get(
                    "https://api.thingiverse.com/v2/search/things",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5,
                )
                return r

            fut_all = _SEARCH_EXECUTOR.submit(_fetch_tv_slice, None)
            fut_cc = _SEARCH_EXECUTOR.submit(_fetch_tv_slice, "cc")
            fut_cc_sa = _SEARCH_EXECUTOR.submit(_fetch_tv_slice, "cc-sa")

            rs = fut_all.result()
            if rs.status_code == 401:
                global _TV_TOKEN_EXPIRES_AT
                _TV_TOKEN_EXPIRES_AT = 0.0
                token = _get_thingiverse_token()
                if token:
                    rs = _fetch_tv_slice(None)

            cc_ids = set()
            cc_sa_ids = set()
            try:
                r_cc = fut_cc.result(timeout=4)
                if r_cc.status_code == 200:
                    cc_ids = {h.get("id") for h in (r_cc.json().get("hits") or []) if h.get("id")}
            except Exception:
                pass
            try:
                r_cc_sa = fut_cc_sa.result(timeout=4)
                if r_cc_sa.status_code == 200:
                    cc_sa_ids = {h.get("id") for h in (r_cc_sa.json().get("hits") or []) if h.get("id")}
            except Exception:
                pass

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
                    if thing_id in cc_ids:
                        tv_comm = True
                        tv_code = "CC BY"
                        tv_label = "Creative Commons Attribution (CC BY) — комерційний продаж друку дозволено"
                    elif thing_id in cc_sa_ids:
                        tv_comm = True
                        tv_code = "CC BY-SA"
                        tv_label = "Creative Commons Attribution-ShareAlike (CC BY-SA) — комерційний продаж друку дозволено"
                    else:
                        tv_comm = False
                        tv_code = "Non-Commercial"
                        tv_label = "Некомерційна ліцензія (CC BY-NC) або обмежена — перевірте на Thingiverse"
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
                        "commercial_allowed": tv_comm,
                        "license_code": tv_code,
                        "license_label": tv_label,
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
                "commercial_allowed": None,
                "license_code": "Див. ліцензію",
                "license_label": "Перевірте ліцензію на сторінці Thingiverse",
                "similarity": max(70, 91 - len(results)),
            })
            if len(results) >= limit:
                break

    _set_cached("thingiverse", query, limit, results)
    return results


def search_multi_queries_on_site(search_fn, queries: List[str], limit_total: int = 50) -> List[Dict[str, Any]]:
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

    per_q = max(24, (limit_total + len(unique_queries) - 1) // len(unique_queries))
    merged: List[Dict[str, Any]] = []
    seen_ids = set()

    futs = [_SEARCH_EXECUTOR.submit(search_fn, q, per_q) for q in unique_queries]
    for f in futs:
        try:
            for item in f.result(timeout=7):
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
    limit_per_site: int = 50,
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

    future_to_site = {
        _SEARCH_EXECUTOR.submit(search_multi_queries_on_site, fn, queries_to_use, limit_per_site): site_key
        for site_key, fn in other_sites.items()
    }

    if precomputed_makerworld_visual is not None and len(precomputed_makerworld_visual) > 0:
        mw_text_fut = _SEARCH_EXECUTOR.submit(search_multi_queries_on_site, search_makerworld, queries_to_use, 30)
        mw_items = [dict(x) for x in precomputed_makerworld_visual]
        seen_mw = {x["id"] for x in mw_items}
        try:
            for extra in mw_text_fut.result(timeout=6):
                if extra["id"] not in seen_mw:
                    seen_mw.add(extra["id"])
                    mw_items.append(dict(extra))
        except Exception:
            pass
        counts_by_site["makerworld"] = len(mw_items)
        all_items.extend(mw_items)
    else:
        future_to_site[_SEARCH_EXECUTOR.submit(search_multi_queries_on_site, search_makerworld, queries_to_use, limit_per_site)] = "makerworld"

    for future in concurrent.futures.as_completed(future_to_site):
        site_key = future_to_site[future]
        try:
            site_results = future.result(timeout=7)
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


# ==============================================================================
# Model Specs for Instant 3D Printing Cost Calculator
# ==============================================================================
_MODEL_SPECS_CACHE: Dict[str, tuple] = {}
_SPECS_LOCK = threading.Lock()
_SPECS_TTL = 3600  # 1 hour cache


def get_model_specs(site: str, model_id: str, title: str = "") -> Dict[str, Any]:
    """
    Retrieves or calculates estimated 3D printing specs (filament weight, print time, material)
    for a given 3D model to feed directly into the STL-HUB cost calculator.
    """
    site_key = (site or "").lower().strip()
    mid = str(model_id or "").strip()
    clean_title = (title or "").strip()
    cache_key = f"{site_key}:{mid}:{clean_title.lower()}"
    now = time.time()

    with _SPECS_LOCK:
        if cache_key in _MODEL_SPECS_CACHE:
            ts, cached_val = _MODEL_SPECS_CACHE[cache_key]
            if now - ts < _SPECS_TTL:
                return dict(cached_val)

    res = _fetch_model_specs(site_key, mid, clean_title)

    with _SPECS_LOCK:
        if len(_MODEL_SPECS_CACHE) > 500:
            _MODEL_SPECS_CACHE.clear()
        _MODEL_SPECS_CACHE[cache_key] = (now, dict(res))

    return res


def _fetch_model_specs(site_key: str, mid: str, title: str = "") -> Dict[str, Any]:
    # 1. MakerWorld Native Sliced Profiles
    if "makerworld" in site_key:
        clean_id = mid.replace("makerworld_", "").split("-")[0]
        if clean_id.isdigit():
            try:
                sess = create_session()
                url = f"https://makerworld.com/api/v1/design-service/design/{clean_id}"
                resp = sess.get(url, headers=_MW_HEADERS, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    instances = data.get("instances") or []
                    if instances:
                        inst = instances[0]
                        weight = float(inst.get("weight") or 0)
                        prediction_sec = int(inst.get("prediction") or 0)
                        filaments = inst.get("instanceFilaments") or []
                        mat_type = filaments[0].get("type") if filaments else "PLA"
                        hours = prediction_sec // 3600
                        minutes = (prediction_sec % 3600) // 60
                        if weight > 0:
                            return {
                                "status": "ok",
                                "weight_g": round(weight, 1),
                                "print_hours": hours,
                                "print_minutes": minutes,
                                "material_type": (mat_type or "PLA").upper(),
                                "is_estimate": False,
                                "source": "MakerWorld Profile"
                            }
            except Exception as e:
                print(f"[Model Specs MakerWorld Error] {e}")

    # 2. Printables Sliced G-Codes
    elif "printables" in site_key:
        clean_id = mid.replace("printables_", "").split("-")[0]
        if clean_id.isdigit():
            try:
                sess = create_session()
                url = "https://api.printables.com/graphql/"
                gql = """
                query GetPrint($id: ID!) {
                  print(id: $id) {
                    weight
                    gcodes { weight }
                  }
                }
                """
                resp = sess.post(url, json={"query": gql, "variables": {"id": clean_id}}, timeout=5)
                if resp.status_code == 200:
                    p = resp.json().get("data", {}).get("print") or {}
                    weight_str = p.get("weight")
                    weight = float(weight_str) if weight_str else 0.0
                    gcodes = p.get("gcodes") or []
                    if not weight and gcodes:
                        weight = float(gcodes[0].get("weight") or 0.0)
                    if weight > 0:
                        total_mins = max(15, int(weight * 3))
                        return {
                            "status": "ok",
                            "weight_g": round(weight, 1),
                            "print_hours": total_mins // 60,
                            "print_minutes": total_mins % 60,
                            "material_type": "PLA",
                            "is_estimate": False,
                            "source": "Printables G-Code"
                        }
            except Exception as e:
                print(f"[Model Specs Printables Error] {e}")

    # 3. Cross-search MakerWorld for matching print profile by title
    if title and len(title.strip()) >= 3:
        try:
            sess = create_session()
            q_clean = extract_keywords_from_title(title)
            if not q_clean or len(q_clean) < 3:
                q_clean = " ".join([w for w in re.sub(r"[^a-zA-Z0-9\s]", " ", title).split() if len(w) >= 3][:3])
            if q_clean:
                q_enc = urllib.parse.quote(q_clean)
                url = f"https://makerworld.com/api/v1/search-service/select/design2?keyword={q_enc}&offset=0&limit=2"
                resp = sess.get(url, headers=_MW_HEADERS, timeout=4)
                if resp.status_code == 200:
                    hits = resp.json().get("hits") or []
                    for h in hits:
                        mw_id = str(h.get("id") or "")
                        if mw_id.isdigit():
                            mw_specs = get_model_specs("makerworld", mw_id)
                            if mw_specs and mw_specs.get("weight_g") and not mw_specs.get("is_estimate"):
                                return {
                                    "status": "ok",
                                    "weight_g": mw_specs["weight_g"],
                                    "print_hours": mw_specs["print_hours"],
                                    "print_minutes": mw_specs["print_minutes"],
                                    "material_type": mw_specs.get("material_type", "PLA"),
                                    "is_estimate": False,
                                    "source": "Слайс-профіль моделі"
                                }
        except Exception as e:
            print(f"[Cross Search MW Error] {e}")

    # 4. Intelligent Category & Title Heuristics (Distinct weight per model)
    return estimate_by_title_heuristics(title or "3D Model", mid)


def estimate_by_title_heuristics(title: str, model_id: str = "") -> Dict[str, Any]:
    import hashlib
    t = (title or "").lower()

    # Deterministic variance modifier (+- 15%) so distinct models have distinct weights
    h = int(hashlib.md5(f"{title}_{model_id}".encode("utf-8")).hexdigest()[:6], 16)
    variance = 0.88 + (h % 25) / 100.0  # 0.88 to 1.13

    # Priority-ordered categories
    rules = [
        # Micro / Tiny
        (["keychain", "key ring", "cable clip", "clip", "whistle", "pick", "button", "knob", "cap", "spacer"], 10),
        # Calibration / Benchmarks
        (["benchy", "calibration", "test cube", "xyz cube"], 14),
        # Tabletop / Games
        (["dice", "d&d", "miniature", "token", "chess", "pawn"], 18),
        # Small flat items
        (["coaster", "fidget", "spinner", "card", "tag", "bookmark"], 24),
        # Large articulated / Dragons / Animals
        (["articulated", "flexi", "dragon", "lizard", "snake", "dinosaur", "t-rex", "crocodile"], 120),
        # Cosplay / Helmets / Props
        (["helmet", "mask", "armor", "shield", "cosplay", "sword", "dagger", "prop", "wand"], 270),
        # Storage / Organization
        (["organizer", "gridfinity", "drawer", "tool rack", "bin", "storage box", "tray"], 135),
        # Vases / Pots
        (["vase", "planter", "flower pot", "pot", "bowl", "cup"], 85),
        # Lighting / Lamps
        (["lampshade", "lithophane", "lamp", "desk lamp", "lantern"], 95),
        # Stands & Mounts
        (["phone stand", "phone holder", "tablet stand", "headphone stand", "stand", "holder", "dock"], 55),
        # Hardware & Mechanisms
        (["bolt", "latch", "lock", "hinge", "bracket", "mount", "handle", "sliding", "clamp", "gear"], 42),
        # Cases & Enclosures
        (["case", "enclosure", "housing", "box"], 60),
        # Sculptures & Figures
        (["statue", "bust", "sculpture", "figure", "figurine"], 75),
    ]

    base_weight = 45.0
    for keywords, weight in rules:
        if any(kw in t for kw in keywords):
            base_weight = weight
            break

    final_weight = round(base_weight * variance, 1)

    # Modern FDM print time: ~20g/hr (approx 3.1 mins per gram)
    total_mins = max(18, int(final_weight * 3.1))
    hours = total_mins // 60
    minutes = total_mins % 60

    return {
        "status": "ok",
        "weight_g": final_weight,
        "print_hours": hours,
        "print_minutes": minutes,
        "material_type": "PLA",
        "is_estimate": True,
        "source": "Орієнтовний розрахунок STL-HUB"
    }

