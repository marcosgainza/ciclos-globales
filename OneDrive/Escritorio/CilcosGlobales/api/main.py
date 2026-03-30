"""
MGI API — Marcos Gainza Index
Servidor REST que expone el MGI Score en tiempo real.
Framework: FastAPI
Deploy: Railway / Render / Fly.io (gratis)

Uso local:
  pip install fastapi uvicorn requests mercadopago
  uvicorn main:app --reload

Endpoints:
  GET /              → info
  GET /v1/score      → MGI Score completo
  GET /v1/indicators → solo los 5 indicadores
  GET /v1/market     → solo datos de mercado
  GET /v1/countries  → score por país
  POST /v1/create-preference → crear checkout MercadoPago
  POST /v1/webhook/mercadopago → webhook de pagos
  GET /payment-success → página post-pago con API key
"""

import math
import time
import threading
import sqlite3
import secrets
import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

try:
    import requests as req
except ImportError:
    raise ImportError("pip install requests")

try:
    import mercadopago
except ImportError:
    raise ImportError("pip install mercadopago")

# ════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════
FRED_KEY = "af1b5c3f8dc1ac7a964de55ab5a7d1fd"
UPDATE_INTERVAL = 30  # seconds

# API Keys — hardcoded (siempre válidas)
API_KEYS = {
    "mgi-demo-key-2026",
    "mgi-marcos-master-key",
}

REQUIRE_AUTH = False  # Cambiar a True cuando quieras exigir API key

# MercadoPago
MP_ACCESS_TOKEN = os.getenv(
    "MP_ACCESS_TOKEN",
    "APP_USR-3227082155784399-022520-b5f8073673d3e6786676e6f08c0c88c1-136606559",
)
RAILWAY_URL = os.getenv(
    "RAILWAY_URL",
    "https://ciclos-globales-production.up.railway.app",
)
sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

# ════════════════════════════════════════════
# DATABASE (SQLite)
# ════════════════════════════════════════════
DB_PATH = os.getenv("DB_PATH", "/tmp/mgi_keys.db")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            email TEXT,
            mp_payment_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)
    db.commit()
    db.close()


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


init_db()

# ════════════════════════════════════════════
# APP
# ════════════════════════════════════════════
app = FastAPI(
    title="MGI — Marcos Gainza Index",
    description="API de ciclos económicos globales en tiempo real",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ════════════════════════════════════════════
# AUTH
# ════════════════════════════════════════════
def verify_key(authorization: str = Header(default=None)):
    if not REQUIRE_AUTH:
        return True
    if not authorization:
        raise HTTPException(401, "API key required. Header: Authorization: Bearer <key>")
    key = authorization.replace("Bearer ", "").strip()
    if key in API_KEYS:
        return True
    db = get_db()
    row = db.execute("SELECT active FROM api_keys WHERE key = ?", (key,)).fetchone()
    db.close()
    if row and row["active"]:
        return True
    raise HTTPException(403, "Invalid API key")


# ════════════════════════════════════════════
# FORMULA & MGI ENGINE
# ════════════════════════════════════════════
P = {"A": 30.0, "omega": 0.52, "phi": 0.94, "b": 2.1, "sigma": 5.0, "delta": 0.012}
t_val = 4.0
history = []

DATA = {
    "btc": None, "btc_chg": None,
    "sp500": None, "sp500_chg": None,
    "inflation": None, "fed_rate": None,
    "blue": None, "oficial": None, "brecha": None,
    "riesgo_pais": None,
    "ar_inflacion_mensual": None, "ar_inflacion_anual": None,
    "ar_tasa_pm": None, "ar_reservas": None,
}

LAST_UPDATE = {"ts": None}


def y_clean(t):
    d = math.exp(-P["delta"] * abs(math.sin(P["omega"] * t + P["phi"])))
    return P["A"] * d * math.sin(P["omega"] * t + P["phi"]) + P["b"] * t


def ma(n):
    if len(history) < n:
        return None
    return sum(history[-n:]) / n


def update_params():
    if DATA["btc_chg"] is not None:
        P["A"] = max(15, min(55, 20 + abs(DATA["btc_chg"]) * 1.8))
        P["phi"] = 0.94 + (DATA["btc_chg"] / 10) * 0.25
    if DATA["inflation"] is not None:
        P["b"] = max(0.5, 4.0 - float(DATA["inflation"]) * 0.13)
    if DATA["brecha"] is not None:
        P["sigma"] = min(22, DATA["brecha"] * 0.07)
    elif DATA["btc_chg"] is not None:
        P["sigma"] = abs(DATA["btc_chg"]) * 0.7
    P["delta"] = max(0.005, min(0.06, P["sigma"] * 0.003))


def calc_mgi():
    global t_val
    t_val += 0.003 * UPDATE_INTERVAL
    yt = y_clean(t_val)
    history.append(yt)
    if len(history) > 30:
        history.pop(0)

    I1 = -math.sin(P["omega"] * t_val + P["phi"])

    yt_prev = y_clean(t_val - 0.1)
    diff = yt - yt_prev
    I2 = 1.0 if diff > 0.01 else (-1.0 if diff < -0.01 else 0.0)

    mu = ma(20)
    I3 = max(-1, min(1, -((yt - mu) / max(P["A"], 1)))) if mu is not None else 0.0

    I4 = max(0, min(1, 1 - (P["sigma"] / 25)))

    ma5, ma20 = ma(5), ma(20)
    if ma5 is not None and ma20 is not None:
        I5 = 1.0 if ma5 > ma20 else (-1.0 if ma5 < ma20 else 0.0)
    else:
        I5 = 0.0

    raw = (I1 + I2 + I3 + I5) / 4
    score = raw * I4
    is_collapse = P["sigma"] > P["A"]

    if is_collapse:
        signal = "COLAPSO_SISTEMICO"
    elif score >= 0.40:
        signal = "ACUMULACION"
    elif score >= -0.40:
        signal = "NEUTRAL"
    else:
        signal = "DISTRIBUCION"

    return {
        "mgi_score": round(score, 4),
        "signal": signal,
        "is_collapse": is_collapse,
        "indicators": {
            "I1_posicion_seno": round(I1, 4),
            "I2_pendiente": round(I2, 4),
            "I3_dist_media": round(I3, 4),
            "I4_confiabilidad": round(I4, 4),
            "I5_cruce_medias": round(I5, 4),
        },
        "formula": {
            "y_t": round(yt, 4),
            "params": {k: round(v, 4) for k, v in P.items()},
        },
    }


# ════════════════════════════════════════════
# DATA FETCHERS
# ════════════════════════════════════════════
def fetch_btc():
    try:
        r = req.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=10,
        )
        d = r.json()
        DATA["btc"] = d["bitcoin"]["usd"]
        DATA["btc_chg"] = round(d["bitcoin"]["usd_24h_change"], 2)
    except Exception:
        pass


def fetch_fred():
    try:
        r = req.get(
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=CPIAUCSL&api_key={FRED_KEY}"
            f"&sort_order=desc&limit=2&file_type=json",
            timeout=10,
        )
        obs = r.json().get("observations", [])
        if len(obs) >= 2:
            last, prev = float(obs[0]["value"]), float(obs[1]["value"])
            DATA["inflation"] = round(((last - prev) / prev) * 100 * 12, 1)
    except Exception:
        pass
    try:
        r = req.get(
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=FEDFUNDS&api_key={FRED_KEY}"
            f"&sort_order=desc&limit=1&file_type=json",
            timeout=10,
        )
        obs = r.json().get("observations", [])
        if obs:
            DATA["fed_rate"] = float(obs[0]["value"])
    except Exception:
        pass


def fetch_argentina():
    try:
        r = req.get("https://api.bluelytics.com.ar/v2/latest", timeout=10)
        d = r.json()
        DATA["blue"] = d["blue"]["value_sell"]
        DATA["oficial"] = d["oficial"]["value_sell"]
        DATA["brecha"] = round(((DATA["blue"] - DATA["oficial"]) / DATA["oficial"]) * 100, 1)
    except Exception:
        pass
    try:
        r = req.get("https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais/ultimo", timeout=10)
        DATA["riesgo_pais"] = r.json().get("valor")
    except Exception:
        pass
    try:
        r = req.get("https://api.argentinadatos.com/v1/finanzas/indices/inflacion", timeout=10)
        d = r.json()
        if d:
            DATA["ar_inflacion_mensual"] = round(float(d[-1]["valor"]), 1)
    except Exception:
        pass
    try:
        r = req.get("https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/28", timeout=10)
        det = r.json().get("results", [{}])[0].get("detalle", [])
        if det:
            DATA["ar_inflacion_anual"] = round(float(det[-1]["valor"]), 1)
    except Exception:
        pass
    try:
        r = req.get("https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/160", timeout=10)
        det = r.json().get("results", [{}])[0].get("detalle", [])
        if det:
            DATA["ar_tasa_pm"] = round(float(det[-1]["valor"]), 1)
    except Exception:
        pass
    try:
        r = req.get("https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/1", timeout=10)
        det = r.json().get("results", [{}])[0].get("detalle", [])
        if det:
            DATA["ar_reservas"] = round(float(det[-1]["valor"]), 0)
    except Exception:
        pass


def fetch_all():
    fetch_btc()
    fetch_fred()
    fetch_argentina()
    update_params()
    LAST_UPDATE["ts"] = datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════
# BACKGROUND UPDATER
# ════════════════════════════════════════════
def updater_loop():
    for i in range(25):
        yt = y_clean(t_val - (25 - i) * 0.1)
        history.append(yt)

    fetch_all()
    while True:
        time.sleep(UPDATE_INTERVAL)
        fetch_all()


@app.on_event("startup")
def startup():
    thread = threading.Thread(target=updater_loop, daemon=True)
    thread.start()


# ════════════════════════════════════════════
# ENDPOINTS — MGI
# ════════════════════════════════════════════
@app.get("/")
def root():
    return {
        "name": "MGI — Marcos Gainza Index",
        "version": "1.0.0",
        "author": "Marcos Gainza",
        "endpoints": ["/v1/score", "/v1/indicators", "/v1/market", "/v1/countries"],
        "docs": "/docs",
    }


@app.get("/v1/score")
def score(authorization: str = Header(default=None)):
    verify_key(authorization)
    mgi = calc_mgi()
    return {
        **mgi,
        "market_data": {
            "btc_usd": DATA["btc"],
            "btc_change_24h": DATA["btc_chg"],
            "inflation_usa": DATA["inflation"],
            "fed_rate": DATA["fed_rate"],
            "blue_ars": DATA["blue"],
            "oficial_ars": DATA["oficial"],
            "brecha_ars": DATA["brecha"],
            "riesgo_pais_arg": DATA["riesgo_pais"],
        },
        "updated_at": LAST_UPDATE["ts"],
    }


@app.get("/v1/indicators")
def indicators(authorization: str = Header(default=None)):
    verify_key(authorization)
    mgi = calc_mgi()
    return {
        "mgi_score": mgi["mgi_score"],
        "signal": mgi["signal"],
        "indicators": mgi["indicators"],
        "formula_mgi": "MGI = [(I1+I2+I3+I5)/4] * conf(epsilon)",
        "updated_at": LAST_UPDATE["ts"],
    }


@app.get("/v1/market")
def market(authorization: str = Header(default=None)):
    verify_key(authorization)
    return {
        "global": {
            "btc_usd": DATA["btc"],
            "btc_change_24h": DATA["btc_chg"],
            "sp500": DATA["sp500"],
            "inflation_usa": DATA["inflation"],
            "fed_rate": DATA["fed_rate"],
        },
        "argentina": {
            "blue_ars": DATA["blue"],
            "oficial_ars": DATA["oficial"],
            "brecha": DATA["brecha"],
            "riesgo_pais": DATA["riesgo_pais"],
            "inflacion_mensual": DATA["ar_inflacion_mensual"],
            "inflacion_anual": DATA["ar_inflacion_anual"],
            "tasa_politica": DATA["ar_tasa_pm"],
            "reservas_usd_mm": DATA["ar_reservas"],
        },
        "updated_at": LAST_UPDATE["ts"],
    }


@app.get("/v1/countries")
def countries(authorization: str = Header(default=None)):
    verify_key(authorization)

    def country_phase(crisis_score):
        if crisis_score > 75:
            return "CRISIS"
        if crisis_score > 55:
            return "CORRECCION"
        if crisis_score > 40:
            return "RECUPERACION"
        if crisis_score > 25:
            return "EXPANSION"
        return "EUFORIA"

    ar_crisis = 50
    if DATA["brecha"] is not None and DATA["riesgo_pais"] is not None:
        ar_crisis = min(100, int(
            (min(100, DATA["brecha"] * 0.8 + 20) * 0.25) +
            (min(100, DATA["riesgo_pais"] / 20) * 0.35) +
            (min(100, float(DATA["ar_inflacion_anual"] or 40) * 0.6) * 0.25) +
            (min(100, float(DATA["ar_tasa_pm"] or 40) * 1.2) * 0.15)
        ))

    return {
        "countries": [
            {"id": "ar", "name": "Argentina", "crisis_score": ar_crisis, "phase": country_phase(ar_crisis),
             "key_indicator": f"Riesgo País: {DATA['riesgo_pais'] or '...'} pts"},
            {"id": "us", "name": "Estados Unidos", "crisis_score": min(100, max(0, int((float(DATA['inflation'] or 3) - 2) * 15))),
             "phase": country_phase(min(100, max(0, int((float(DATA['inflation'] or 3) - 2) * 15)))),
             "key_indicator": f"FED: {DATA['fed_rate'] or '...'}%"},
        ],
        "mgi_score": calc_mgi()["mgi_score"],
        "updated_at": LAST_UPDATE["ts"],
    }


# ════════════════════════════════════════════
# ENDPOINTS — MERCADOPAGO PAGOS
# ════════════════════════════════════════════
@app.post("/v1/create-preference")
def create_preference():
    preference_data = {
        "items": [
            {
                "title": "MGI API — Licencia Mensual",
                "quantity": 1,
                "unit_price": 29,
                "currency_id": "ARS",
            }
        ],
        "back_urls": {
            "success": f"{RAILWAY_URL}/payment-success",
            "failure": f"{RAILWAY_URL}/payment-failure",
            "pending": f"{RAILWAY_URL}/payment-pending",
        },
        "auto_return": "approved",
        "notification_url": f"{RAILWAY_URL}/v1/webhook/mercadopago",
    }
    result = sdk.preference().create(preference_data)
    preference = result["response"]
    return {"init_point": preference["init_point"], "id": preference["id"]}


def _generate_key():
    return f"mgi-{secrets.token_hex(16)}"


def _get_or_create_key(payment_id):
    """Busca una key existente para el payment_id, o la crea si el pago fue aprobado."""
    db = get_db()
    row = db.execute("SELECT key FROM api_keys WHERE mp_payment_id = ?", (str(payment_id),)).fetchone()
    if row:
        db.close()
        return row["key"]

    payment_info = sdk.payment().get(int(payment_id))
    payment = payment_info["response"]

    if payment.get("status") != "approved":
        db.close()
        return None

    api_key = _generate_key()
    email = payment.get("payer", {}).get("email", "unknown")
    try:
        db.execute(
            "INSERT INTO api_keys (key, email, mp_payment_id, created_at) VALUES (?, ?, ?, ?)",
            (api_key, email, str(payment_id), datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        row = db.execute("SELECT key FROM api_keys WHERE mp_payment_id = ?", (str(payment_id),)).fetchone()
        api_key = row["key"] if row else None
    db.close()
    return api_key


@app.post("/v1/webhook/mercadopago")
async def mercadopago_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"status": "ok"}, status_code=200)

    if body.get("type") == "payment":
        payment_id = body.get("data", {}).get("id")
        if payment_id:
            _get_or_create_key(payment_id)

    return JSONResponse(content={"status": "ok"}, status_code=200)


SUCCESS_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MGI — API Key Activada</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=Syne+Mono&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0A0A0F;color:#EEF0FF;font-family:'Syne',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:rgba(201,162,39,0.06);border:1px solid rgba(201,162,39,0.25);border-radius:20px;padding:48px 40px;max-width:540px;width:100%;text-align:center}
.badge{display:inline-block;font-family:'Syne Mono',monospace;font-size:9px;letter-spacing:0.3em;color:#C9A227;border:1px solid rgba(201,162,39,0.3);border-radius:20px;padding:6px 16px;margin-bottom:24px}
h1{font-size:28px;font-weight:800;margin-bottom:10px}
.sub{font-family:'Syne Mono',monospace;font-size:12px;color:rgba(238,240,255,0.5);margin-bottom:32px}
.key-label{font-family:'Syne Mono',monospace;font-size:9px;letter-spacing:0.25em;color:rgba(238,240,255,0.4);margin-bottom:10px}
.key-box{background:rgba(0,232,122,0.08);border:1px solid rgba(0,232,122,0.3);border-radius:10px;padding:18px 20px;font-family:'Syne Mono',monospace;font-size:14px;color:#00E87A;word-break:break-all;cursor:pointer;transition:background 0.2s;position:relative}
.key-box:hover{background:rgba(0,232,122,0.14)}
.key-box::after{content:'click para copiar';position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-size:9px;color:rgba(238,240,255,0.3);letter-spacing:0.1em}
.usage{margin-top:40px;text-align:left;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:20px}
.usage-title{font-family:'Syne Mono',monospace;font-size:9px;letter-spacing:0.2em;color:rgba(238,240,255,0.4);margin-bottom:12px}
.usage code{display:block;font-family:'Syne Mono',monospace;font-size:11px;color:rgba(238,240,255,0.7);line-height:1.8;white-space:pre-wrap}
.back{display:inline-block;margin-top:32px;font-family:'Syne Mono',monospace;font-size:11px;color:#C9A227;text-decoration:none;border:1px solid rgba(201,162,39,0.3);border-radius:8px;padding:10px 24px;transition:background 0.2s}
.back:hover{background:rgba(201,162,39,0.1)}
.copied{position:fixed;top:20px;left:50%;transform:translateX(-50%);background:#00E87A;color:#0A0A0F;font-family:'Syne Mono',monospace;font-size:12px;font-weight:700;padding:10px 24px;border-radius:8px;opacity:0;transition:opacity 0.3s;pointer-events:none}
.copied.show{opacity:1}
</style>
</head>
<body>
<div class="card">
  <div class="badge">MGI — MARCOS GAINZA INDEX</div>
  <h1>Pago Confirmado</h1>
  <p class="sub">Tu API key fue generada automaticamente</p>
  <div class="key-label">TU API KEY</div>
  <div class="key-box" id="keyBox" onclick="copyKey()">{{API_KEY}}</div>
  <div class="usage">
    <div class="usage-title">COMO USAR</div>
    <code>curl -H "Authorization: Bearer {{API_KEY}}" \\
  {{BASE_URL}}/v1/score</code>
  </div>
  <a href="{{BASE_URL}}" class="back">VOLVER AL MGI</a>
</div>
<div class="copied" id="copied">KEY COPIADA</div>
<script>
function copyKey(){
  navigator.clipboard.writeText('{{API_KEY}}');
  var el=document.getElementById('copied');
  el.classList.add('show');
  setTimeout(function(){el.classList.remove('show')},1500);
}
</script>
</body>
</html>"""

FAILURE_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MGI — Pago fallido</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=Syne+Mono&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0A0A0F;color:#EEF0FF;font-family:'Syne',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:rgba(255,45,85,0.06);border:1px solid rgba(255,45,85,0.25);border-radius:20px;padding:48px 40px;max-width:540px;width:100%;text-align:center}
h1{font-size:28px;font-weight:800;margin-bottom:10px;color:#FF2D55}
.sub{font-family:'Syne Mono',monospace;font-size:12px;color:rgba(238,240,255,0.5);margin-bottom:24px}
.back{display:inline-block;font-family:'Syne Mono',monospace;font-size:11px;color:#C9A227;text-decoration:none;border:1px solid rgba(201,162,39,0.3);border-radius:8px;padding:10px 24px;transition:background 0.2s}
.back:hover{background:rgba(201,162,39,0.1)}
</style>
</head>
<body>
<div class="card">
  <h1>Pago no completado</h1>
  <p class="sub">El pago fue rechazado o cancelado. Podes intentar de nuevo.</p>
  <a href="{{BASE_URL}}" class="back">VOLVER AL MGI</a>
</div>
</body>
</html>"""

PENDING_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MGI — Pago pendiente</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=Syne+Mono&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0A0A0F;color:#EEF0FF;font-family:'Syne',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:rgba(201,162,39,0.06);border:1px solid rgba(201,162,39,0.25);border-radius:20px;padding:48px 40px;max-width:540px;width:100%;text-align:center}
h1{font-size:28px;font-weight:800;margin-bottom:10px;color:#C9A227}
.sub{font-family:'Syne Mono',monospace;font-size:12px;color:rgba(238,240,255,0.5);margin-bottom:24px;line-height:1.8}
.back{display:inline-block;font-family:'Syne Mono',monospace;font-size:11px;color:#C9A227;text-decoration:none;border:1px solid rgba(201,162,39,0.3);border-radius:8px;padding:10px 24px;transition:background 0.2s}
.back:hover{background:rgba(201,162,39,0.1)}
</style>
</head>
<body>
<div class="card">
  <h1>Pago Pendiente</h1>
  <p class="sub">Tu pago esta siendo procesado. Cuando se acredite, vas a recibir tu API key.<br>Contactanos si tarda mas de 24hs: <strong>@gainzamarcosia</strong></p>
  <a href="{{BASE_URL}}" class="back">VOLVER AL MGI</a>
</div>
</body>
</html>"""


@app.get("/payment-success", response_class=HTMLResponse)
def payment_success(payment_id: str = None, collection_id: str = None):
    pid = payment_id or collection_id
    api_key = None

    if pid:
        api_key = _get_or_create_key(pid)

    if not api_key:
        return HTMLResponse(
            content=PENDING_HTML.replace("{{BASE_URL}}", RAILWAY_URL),
            status_code=200,
        )

    html = SUCCESS_HTML.replace("{{API_KEY}}", api_key).replace("{{BASE_URL}}", RAILWAY_URL)
    return HTMLResponse(content=html, status_code=200)


@app.get("/payment-failure", response_class=HTMLResponse)
def payment_failure():
    return HTMLResponse(
        content=FAILURE_HTML.replace("{{BASE_URL}}", RAILWAY_URL),
        status_code=200,
    )


@app.get("/payment-pending", response_class=HTMLResponse)
def payment_pending():
    return HTMLResponse(
        content=PENDING_HTML.replace("{{BASE_URL}}", RAILWAY_URL),
        status_code=200,
    )


# ════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
