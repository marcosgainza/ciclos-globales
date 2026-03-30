"""
MGI API — Marcos Gainza Index
Servidor REST que expone el MGI Score en tiempo real.
Framework: FastAPI
Deploy: Railway / Render / Fly.io (gratis)

Uso local:
  pip install fastapi uvicorn requests
  uvicorn main:app --reload

Endpoints:
  GET /              → info
  GET /v1/score      → MGI Score completo
  GET /v1/indicators → solo los 5 indicadores
  GET /v1/market     → solo datos de mercado
  GET /v1/countries  → score por país
"""

import math
import time
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

try:
    import requests as req
except ImportError:
    raise ImportError("pip install requests")

# ════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════
FRED_KEY = "af1b5c3f8dc1ac7a964de55ab5a7d1fd"
UPDATE_INTERVAL = 30  # seconds

# API Keys — agregá las keys de tus clientes acá
# En producción usarías una base de datos
API_KEYS = {
    "mgi-demo-key-2026",       # key de demo
    "mgi-marcos-master-key",   # tu key personal
}

# Set to False to disable API key requirement (for testing)
REQUIRE_AUTH = False

app = FastAPI(
    title="MGI — Marcos Gainza Index",
    description="API de ciclos económicos globales en tiempo real",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
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
    if key not in API_KEYS:
        raise HTTPException(403, "Invalid API key")
    return True


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
    # Pre-fill history
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
# ENDPOINTS
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
# RUN
# ════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
