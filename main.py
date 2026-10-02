import os
import time
import asyncio
import secrets as secrets_module

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

INVENTREE_URL = os.environ["INVENTREE_URL"].rstrip("/")
INVENTREE_TOKEN = os.environ["INVENTREE_TOKEN"]
BASIC_AUTH_USER = os.environ["BASIC_AUTH_USER"]
BASIC_AUTH_PASS = os.environ["BASIC_AUTH_PASS"]

SESSION_COOKIE = "lager_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 Tage

CACHE_TTL = 45
_cache = {"parts": None, "parts_ts": 0.0, "locations": None, "locations_ts": 0.0}
_last_action = None
_sessions: dict[str, float] = {}  # session_id -> Ablauf-Zeitstempel

app = FastAPI()


def create_session() -> str:
    sid = secrets_module.token_urlsafe(32)
    _sessions[sid] = time.time() + SESSION_MAX_AGE
    return sid


def is_valid_session(sid: str | None) -> bool:
    if not sid or sid not in _sessions:
        return False
    if _sessions[sid] < time.time():
        del _sessions[sid]
        return False
    return True


def check_auth(request: Request) -> bool:
    sid = request.cookies.get(SESSION_COOKIE)
    if not is_valid_session(sid):
        raise HTTPException(status_code=401, detail="Nicht angemeldet.")
    return True


@app.get("/login", response_class=HTMLResponse)
async def login_form(error: str = ""):
    with open("/app/login.html", encoding="utf-8") as f:
        html = f.read()
    error_block = (
        '<div class="error">Benutzername oder Passwort falsch.</div>' if error else ""
    )
    return html.replace("__ERROR_BLOCK__", error_block)


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    ok_user = secrets_module.compare_digest(username, BASIC_AUTH_USER)
    ok_pass = secrets_module.compare_digest(password, BASIC_AUTH_PASS)
    if not (ok_user and ok_pass):
        return RedirectResponse(url="/login?error=1", status_code=303)
    sid = create_session()
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, sid, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True
    )
    return resp


@app.post("/logout")
async def logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        _sessions.pop(sid, None)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def inv_headers():
    return {"Authorization": f"Token {INVENTREE_TOKEN}"}


async def fetch_parts(client: httpx.AsyncClient):
    now = time.time()
    if _cache["parts"] is not None and now - _cache["parts_ts"] < CACHE_TTL:
        return _cache["parts"]
    r = await client.get(f"{INVENTREE_URL}/api/part/?active=true", headers=inv_headers())
    r.raise_for_status()
    parts = r.json()
    _cache["parts"] = parts
    _cache["parts_ts"] = now
    return parts


async def fetch_locations(client: httpx.AsyncClient):
    now = time.time()
    if _cache["locations"] is not None and now - _cache["locations_ts"] < CACHE_TTL:
        return _cache["locations"]
    r = await client.get(f"{INVENTREE_URL}/api/stock/location/", headers=inv_headers())
    r.raise_for_status()
    locs = r.json()
    _cache["locations"] = locs
    _cache["locations_ts"] = now
    return locs


async def fetch_stock(client: httpx.AsyncClient):
    r = await client.get(
        f"{INVENTREE_URL}/api/stock/?in_stock=true&ordering=creation_date", headers=inv_headers()
    )
    r.raise_for_status()
    return r.json()


@app.get("/api/locations")
async def api_locations(auth: bool = Depends(check_auth)):
    async with httpx.AsyncClient(timeout=15) as client:
        locs = await fetch_locations(client)
    return [{"pk": l["pk"], "name": l["name"]} for l in locs]


@app.get("/api/parts")
async def api_parts(auth: bool = Depends(check_auth)):
    async with httpx.AsyncClient(timeout=15) as client:
        parts = await fetch_parts(client)
    return [{"IPN": p.get("IPN"), "name": p.get("name")} for p in parts if p.get("IPN")]


@app.post("/api/scan")
async def api_scan(payload: dict, auth: bool = Depends(check_auth)):
    global _last_action
    mode = payload.get("mode")
    barcode = (payload.get("barcode") or "").strip()
    try:
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        quantity = 0
    if quantity < 1:
        return {"success": False, "message": "Ungültige Menge."}

    location = payload.get("location")
    from_location = payload.get("fromLocation")
    to_location = payload.get("toLocation")

    async with httpx.AsyncClient(timeout=15) as client:
        parts, stock, locations = await asyncio.gather(
            fetch_parts(client), fetch_stock(client), fetch_locations(client)
        )
        loc_name = {l["pk"]: l["name"] for l in locations}

        normalized = barcode.upper()
        part = next((p for p in parts if (p.get("IPN") or "").strip().upper() == normalized), None)
        if not part:
            return {"success": False, "message": f'Barcode "{barcode}" nicht erkannt.'}

        if mode == "Einlagern":
            if not location:
                return {"success": False, "message": "Bitte einen Lagerort wählen."}
            location = int(location)
            r = await client.post(
                f"{INVENTREE_URL}/api/stock/",
                headers=inv_headers(),
                json={"part": part["pk"], "location": location, "quantity": quantity},
            )
            if r.status_code >= 400:
                return {"success": False, "message": f"InvenTree-Fehler: {r.text[:200]}"}
            new_item = r.json()
            if isinstance(new_item, list):
                new_item = new_item[0]
            _last_action = {
                "type": "create",
                "stock_pk": new_item["pk"],
                "quantity": quantity,
                "label": f'Eingelagert: {part["name"]} ({loc_name.get(location, location)}, +{quantity})',
            }
            return {"success": True, "message": _last_action["label"]}

        if mode == "Auslagern":
            if not location:
                return {"success": False, "message": "Bitte einen Lagerort wählen."}
            location = int(location)
            items = [
                s for s in stock
                if s["part"] == part["pk"] and s["location"] == location and float(s["quantity"]) > 0
            ]
            if not items:
                return {"success": False, "message": f'Kein Bestand von {part["name"]} am gewählten Lagerort vorhanden.'}
            oldest = items[0]
            if float(oldest["quantity"]) < quantity:
                return {
                    "success": False,
                    "message": f'Nur {oldest["quantity"]} Stück im ältesten Posten von {part["name"]} an '
                    f'{loc_name.get(location)} vorhanden (angefordert: {quantity}).',
                }
            r = await client.post(
                f"{INVENTREE_URL}/api/stock/remove/",
                headers=inv_headers(),
                json={"items": [{"pk": oldest["pk"], "quantity": quantity}]},
            )
            if r.status_code >= 400:
                return {"success": False, "message": f"InvenTree-Fehler: {r.text[:200]}"}
            _last_action = {
                "type": "remove",
                "stock_pk": oldest["pk"],
                "quantity": quantity,
                "label": f'Ausgelagert: {part["name"]} ({loc_name.get(location)}, -{quantity})',
            }
            return {"success": True, "message": _last_action["label"]}

        if mode == "Umlagern":
            if not from_location or not to_location:
                return {"success": False, "message": "Bitte Von- und Nach-Lagerort wählen."}
            from_location = int(from_location)
            to_location = int(to_location)
            if from_location == to_location:
                return {"success": False, "message": "Von- und Nach-Lagerort dürfen nicht identisch sein."}
            items = [
                s for s in stock
                if s["part"] == part["pk"] and s["location"] == from_location and float(s["quantity"]) > 0
            ]
            if not items:
                return {"success": False, "message": f'Kein Bestand von {part["name"]} am gewählten Von-Lagerort vorhanden.'}
            oldest = items[0]
            if float(oldest["quantity"]) < quantity:
                return {
                    "success": False,
                    "message": f'Nur {oldest["quantity"]} Stück im ältesten Posten von {part["name"]} an '
                    f'{loc_name.get(from_location)} vorhanden (angefordert: {quantity}).',
                }
            r = await client.post(
                f"{INVENTREE_URL}/api/stock/transfer/",
                headers=inv_headers(),
                json={"items": [{"pk": oldest["pk"], "quantity": quantity}], "location": to_location},
            )
            if r.status_code >= 400:
                return {"success": False, "message": f"InvenTree-Fehler: {r.text[:200]}"}
            _last_action = {
                "type": "transfer",
                "stock_pk": oldest["pk"],
                "quantity": quantity,
                "from_location": from_location,
                "to_location": to_location,
                "label": f'Umgelagert: {part["name"]} ({loc_name.get(from_location)} → {loc_name.get(to_location)}, {quantity})',
            }
            return {"success": True, "message": _last_action["label"]}

    return {"success": False, "message": f"Unbekannter Modus: {mode}."}


@app.post("/api/undo")
async def api_undo(auth: bool = Depends(check_auth)):
    global _last_action
    if not _last_action:
        return {"success": False, "message": "Nichts zum Rückgängigmachen vorhanden."}
    action = _last_action
    async with httpx.AsyncClient(timeout=15) as client:
        if action["type"] == "create":
            r = await client.post(
                f"{INVENTREE_URL}/api/stock/remove/",
                headers=inv_headers(),
                json={"items": [{"pk": action["stock_pk"], "quantity": action["quantity"]}]},
            )
        elif action["type"] == "remove":
            r = await client.post(
                f"{INVENTREE_URL}/api/stock/add/",
                headers=inv_headers(),
                json={"items": [{"pk": action["stock_pk"], "quantity": action["quantity"]}]},
            )
        elif action["type"] == "transfer":
            r = await client.post(
                f"{INVENTREE_URL}/api/stock/transfer/",
                headers=inv_headers(),
                json={
                    "items": [{"pk": action["stock_pk"], "quantity": action["quantity"]}],
                    "location": action["from_location"],
                },
            )
        else:
            return {"success": False, "message": "Unbekannte Aktion."}

    if r.status_code >= 400:
        return {"success": False, "message": f"Rückgängig fehlgeschlagen: {r.text[:200]}"}
    undone_label = action["label"]
    _last_action = None
    return {"success": True, "message": f"Rückgängig gemacht: {undone_label}"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/login", status_code=303)
    with open("/app/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
async def health():
    return {"status": "ok"}
