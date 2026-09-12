"""Anonymous first. Google sign-in only upgrades an existing anonymous session.

Every visitor gets a uid cookie immediately and can chat with no barrier.
Signing in rebinds their existing sessions and facts to a stable google id, so
nothing is lost in the upgrade.
"""
import os
import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT = os.getenv("OAUTH_REDIRECT", "http://localhost:8000/auth/callback")

router = APIRouter()
_states: dict[str, str] = {}


def enabled() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


@router.get("/auth/google")
def start(request: Request):
    if not enabled():
        raise HTTPException(503, "google sign-in is not configured")
    state = secrets.token_urlsafe(24)
    _states[state] = request.cookies.get("uid", "anon")
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": "openid email", "state": state, "prompt": "select_account"})
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    previous = _states.pop(state, None)
    if previous is None:
        raise HTTPException(400, "bad state")

    async with httpx.AsyncClient() as client:
        token = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT, "grant_type": "authorization_code"},
            timeout=15.0)
        token.raise_for_status()
        info = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            timeout=15.0)
        info.raise_for_status()

    claims = info.json()
    uid = f"g:{claims['sub']}"
    resp = RedirectResponse("/")
    resp.set_cookie("uid", uid, max_age=31_536_000, httponly=True,
                    samesite="lax", secure=REDIRECT.startswith("https"))
    resp.set_cookie("email", claims.get("email", ""), max_age=31_536_000,
                    samesite="lax")
    request.state.migrate = (previous, uid)
    return resp


@router.post("/auth/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("uid")
    resp.delete_cookie("email")
    return resp
