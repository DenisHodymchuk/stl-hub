import json
import os
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, Depends
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import curl_cffi.requests as curl_req

from ai_vision import load_config
from user_db import (
    upsert_google_user,
    create_user_session,
    get_user_by_session_token,
    delete_user_session,
)


def get_google_client_id() -> str:
    cfg = load_config()
    return cfg.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID") or ""


def verify_google_id_token(credential: str) -> Optional[Dict[str, Any]]:
    """
    Verifies a Google ID token (JWT) returned by Google Identity Services.
    Uses google-auth library with fallback to Google's tokeninfo API.
    """
    client_id = get_google_client_id().strip() or None
    payload = None

    # Method 1: Official google.oauth2.id_token
    try:
        req = google_requests.Request()
        # If client_id is known, verify audience. If not configured, verify token authenticity without audience.
        payload = id_token.verify_oauth2_token(
            credential,
            req,
            audience=client_id if client_id else None,
            clock_skew_in_seconds=10,
        )
    except Exception:
        payload = None

    # Method 2: Fallback to Google's tokeninfo endpoint
    if not payload:
        try:
            resp = curl_req.get(f"https://oauth2.googleapis.com/tokeninfo?id_token={credential}", timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                if "sub" in data and "email" in data:
                    if client_id and data.get("aud") != client_id:
                        return None
                    payload = data
        except Exception:
            payload = None

    if not payload or not payload.get("sub"):
        return None

    return {
        "google_id": str(payload.get("sub")),
        "email": str(payload.get("email") or ""),
        "name": str(payload.get("name") or payload.get("given_name") or payload.get("email", "").split("@")[0]),
        "avatar_url": str(payload.get("picture") or ""),
    }


def get_session_token_from_request(request: Request) -> Optional[str]:
    # 1. Cookie
    token = request.cookies.get("stlhub_session")
    if token:
        return token.strip()

    # 2. Authorization header: "Bearer <token>"
    auth_hdr = request.headers.get("authorization") or ""
    if auth_hdr.lower().startswith("bearer "):
        return auth_hdr[7:].strip()

    # 3. Custom header
    x_token = request.headers.get("x-user-token")
    if x_token:
        return x_token.strip()

    return None


async def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    token = get_session_token_from_request(request)
    if not token:
        return None
    return get_user_by_session_token(token)


async def require_current_user(request: Request) -> Dict[str, Any]:
    user = await get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Необхідна авторизація через Google для доступу до цієї дії",
        )
    return user
