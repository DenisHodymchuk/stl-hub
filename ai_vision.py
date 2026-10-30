import io
import json
import os
import re
from typing import Dict, Any, List, Optional
from PIL import Image
from google import genai
from google.genai import types

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_API_KEY = os.environ.get("GEMINI_API_KEY", "")

_GENAI_CLIENTS: Dict[str, genai.Client] = {}


def _get_genai_client(api_key: str) -> genai.Client:
    client = _GENAI_CLIENTS.get(api_key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _GENAI_CLIENTS[api_key] = client
    return client


def load_config() -> Dict[str, Any]:
    cfg = {"gemini_api_key": DEFAULT_API_KEY, "model": "gemini-2.5-flash", "google_client_id": ""}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(api_key: Optional[str] = None, model: Optional[str] = None, google_client_id: Optional[str] = None) -> None:
    existing = load_config()
    if api_key is not None:
        existing["gemini_api_key"] = api_key.strip()
    if model is not None:
        existing["model"] = model.strip()
    if google_client_id is not None:
        existing["google_client_id"] = google_client_id.strip()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def analyze_image_with_gemini(
    image_bytes: bytes,
    user_hint: str = "",
    visual_hints: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cfg = load_config()
    api_key = cfg.get("gemini_api_key") or DEFAULT_API_KEY
    model_name = cfg.get("model") or "gemini-2.5-flash"

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    max_dim = 800
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    jpeg_bytes = buf.getvalue()

    prompt = """You are an expert in 3D printing repositories (Printables, Thingiverse, MakerWorld, Cults3D, Thangs, MyMiniFactory).
Analyze this image to generate the best search queries to find visually and functionally identical 3D models across 3D printing websites.

CRITICAL RULES FOR SEARCH QUERIES:
1. NEVER use letter-shape adjectives like "D-shaped", "C-shaped", "U-shaped", "L-shaped", "round-shaped", "curved" in search queries! Search engines on 3D sites match the literal letter ("D", "C", "U") and return unrelated alphabet beads or rack panels.
2. Instead, use REAL FUNCTIONAL 3D-printing names that creators actually title their uploads with (for example: "sliding door handle", "recessed door pull", "headphone wall mount", "orbital sander wall mount", "angle grinder holder", "pocket handle", "wall hook bracket", "articulated dragon").
3. Keep each query concise (2 to 3 words in English). Do NOT include words like "3d print", "stl", "model".
4. Provide 5 distinct `alternative_queries` covering the most likely real-world uses of this exact geometry so the user can find all visually matching parts.
"""
    if visual_hints:
        top_hints = "\n".join(f"- {h}" for h in visual_hints[:10])
        prompt += f"\n MakerWorld's visual image search matched this photo to these real 3D models:\n{top_hints}\nUse these visual matches and the image itself to pick the most accurate functional search queries!"

    if user_hint and user_hint.strip():
        prompt += f"\nUser hint: '{user_hint.strip()}'."

    prompt += """
Return ONLY valid JSON:
{
  "title_uk": "Точна назва виробу українською",
  "description_uk": "Короткий опис українською (1-2 речення): що це за деталь та для чого використовується",
  "category_uk": "Категорія українською",
  "primary_query": "best 2-3 word functional english query",
  "alternative_queries": ["query 1", "query 2", "query 3", "query 4", "query 5"],
  "detected_features": ["ознака 1", "ознака 2", "ознака 3"]
}
"""

    client = _get_genai_client(api_key)
    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.15,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    parsed = json.loads(raw_text)
    primary = (parsed.get("primary_query") or "wall mount holder").strip()
    # Strip any accidental X-shaped words just in case
    primary = re.sub(r"\b[a-zA-Z]-shaped\b", "", primary, flags=re.I).strip()

    alts = []
    for q in parsed.get("alternative_queries") or []:
        cq = re.sub(r"\b[a-zA-Z]-shaped\b", "", q, flags=re.I).strip()
        if cq and cq.lower() not in [x.lower() for x in alts]:
            alts.append(cq)
    if primary and primary.lower() not in [x.lower() for x in alts]:
        alts.insert(0, primary)

    return {
        "title_uk": parsed.get("title_uk") or "3D Модель",
        "description_uk": parsed.get("description_uk") or "Виріб, розпізнаний за допомогою візуального пошуку та ШІ.",
        "category_uk": parsed.get("category_uk") or "3D Виріб",
        "primary_query": primary,
        "alternative_queries": alts[:6],
        "detected_features": parsed.get("detected_features") or [],
    }
