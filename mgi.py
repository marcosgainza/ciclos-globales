"""
MGI — Marcos Gainza Index
Motor de cálculo en tiempo real
Versión 1.0 · Marzo 2026

Uso: pip install requests colorama
     python mgi.py
"""

import math
import time
import os
import sys
from datetime import datetime

try:
    import requests
except ImportError:
    print("Instalá requests: pip install requests")
    sys.exit(1)

try:
    from colorama import init, Fore, Style
    init()
except ImportError:
    print("Instalá colorama: pip install colorama")
    sys.exit(1)

# ════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════
FRED_KEY = "af1b5c3f8dc1ac7a964de55ab5a7d1fd"
INTERVAL = 30  # segundos entre updates

# ════════════════════════════════════════════
# FORMULA PARAMS
# ════════════════════════════════════════════
P = {
    "A": 30.0,
    "omega": 0.52,
    "phi": 0.94,
    "b": 2.1,
    "sigma": 5.0,
    "delta": 0.012,
}

t = 4.0
history = []  # y(t) history for moving averages

# ════════════════════════════════════════════
# DATA STORE
# ════════════════════════════════════════════
DATA = {
    "btc": None, "btc_chg": None,
    "sp500": None, "sp500_chg": None,
    "inflation": None, "fed_rate": None,
    "blue": None, "oficial": None, "brecha": None,
    "riesgo_pais": None,
}


# ════════════════════════════════════════════
# FORMULA
# ════════════════════════════════════════════
def y_clean(t_val):
    d = math.exp(-P["delta"] * abs(math.sin(P["omega"] * t_val + P["phi"])))
    return P["A"] * d * math.sin(P["omega"] * t_val + P["phi"]) + P["b"] * t_val


def update_params():
    if DATA["btc_chg"] is not None:
        P["A"] = max(15, min(55, 20 + abs(DATA["btc_chg"]) * 1.8))
        P["phi"] = 0.94 + (DATA["btc_chg"] / 10) * 0.25
    if DATA["inflation"] is not None:
        P["b"] = max(0.5, 4.0 - DATA["inflation"] * 0.13)
    if DATA["brecha"] is not None:
        P["sigma"] = min(22, DATA["brecha"] * 0.07)
    elif DATA["btc_chg"] is not None:
        P["sigma"] = abs(DATA["btc_chg"]) * 0.7
    P["delta"] = max(0.005, min(0.06, P["sigma"] * 0.003))


# ════════════════════════════════════════════
# MGI CALCULATION
# ════════════════════════════════════════════
def ma(n):
    if len(history) < n:
        return None
    return sum(history[-n:]) / n


def calc_mgi():
    yt = y_clean(t)
    history.append(yt)
    if len(history) > 30:
        history.pop(0)

    # I1 — Posición en el seno: -sin(wt+phi)
    I1 = -math.sin(P["omega"] * t + P["phi"])

    # I2 — Pendiente: sign(y(t) - y(t-delta))
    yt_prev = y_clean(t - 0.1)
    diff = yt - yt_prev
    I2 = 1.0 if diff > 0.01 else (-1.0 if diff < -0.01 else 0.0)

    # I3 — Distancia a la media: -(y(t) - mu) / A
    mu = ma(20)
    I3 = max(-1, min(1, -((yt - mu) / max(P["A"], 1)))) if mu is not None else 0.0

    # I4 — Confiabilidad: 1 - (sigma/25)
    I4 = max(0, min(1, 1 - (P["sigma"] / 25)))

    # I5 — Cruce medias: sign(MA5 - MA20)
    ma5 = ma(5)
    ma20 = ma(20)
    if ma5 is not None and ma20 is not None:
        I5 = 1.0 if ma5 > ma20 else (-1.0 if ma5 < ma20 else 0.0)
    else:
        I5 = 0.0

    # MGI Score = [(I1+I2+I3+I5)/4] * I4
    raw = (I1 + I2 + I3 + I5) / 4
    score = raw * I4
    is_collapse = P["sigma"] > P["A"]

    return {
        "score": score, "I1": I1, "I2": I2, "I3": I3,
        "I4": I4, "I5": I5, "is_collapse": is_collapse,
        "yt": yt,
    }


# ════════════════════════════════════════════
# API FETCHERS
# ════════════════════════════════════════════
def fetch_btc():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=10,
        )
        d = r.json()
        DATA["btc"] = d["bitcoin"]["usd"]
        DATA["btc_chg"] = d["bitcoin"]["usd_24h_change"]
    except Exception:
        pass


def fetch_fred():
    try:
        r = requests.get(
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=CPIAUCSL&api_key={FRED_KEY}"
            f"&sort_order=desc&limit=2&file_type=json",
            timeout=10,
        )
        d = r.json()
        obs = d.get("observations", [])
        if len(obs) >= 2:
            last = float(obs[0]["value"])
            prev = float(obs[1]["value"])
            DATA["inflation"] = round(((last - prev) / prev) * 100 * 12, 1)
    except Exception:
        pass

    try:
        r = requests.get(
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=FEDFUNDS&api_key={FRED_KEY}"
            f"&sort_order=desc&limit=1&file_type=json",
            timeout=10,
        )
        d = r.json()
        obs = d.get("observations", [])
        if obs:
            DATA["fed_rate"] = float(obs[0]["value"])
    except Exception:
        pass


def fetch_argentina():
    try:
        r = requests.get("https://api.bluelytics.com.ar/v2/latest", timeout=10)
        d = r.json()
        DATA["blue"] = d["blue"]["value_sell"]
        DATA["oficial"] = d["oficial"]["value_sell"]
        DATA["brecha"] = round(
            ((DATA["blue"] - DATA["oficial"]) / DATA["oficial"]) * 100, 1
        )
    except Exception:
        pass

    try:
        r = requests.get(
            "https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais/ultimo",
            timeout=10,
        )
        d = r.json()
        DATA["riesgo_pais"] = d.get("valor")
    except Exception:
        pass


def fetch_all():
    fetch_btc()
    fetch_fred()
    fetch_argentina()
    update_params()


# ════════════════════════════════════════════
# DISPLAY
# ════════════════════════════════════════════
def fmt(v):
    return f"{'+' if v >= 0 else ''}{v:.2f}"


def color_score(v):
    if v > 0.3:
        return Fore.GREEN
    if v > -0.3:
        return Fore.YELLOW
    return Fore.RED


def color_conf(v):
    if v > 0.7:
        return Fore.GREEN
    if v > 0.4:
        return Fore.YELLOW
    return Fore.RED


def render(mgi):
    os.system("cls" if os.name == "nt" else "clear")

    score = mgi["score"]
    is_collapse = mgi["is_collapse"]

    if is_collapse:
        signal = f"{Fore.WHITE}{Style.DIM}COLAPSO SISTEMICO{Style.RESET_ALL}"
        icon = "⚫"
        sc = Fore.WHITE + Style.DIM
    elif score >= 0.40:
        signal = f"{Fore.GREEN}ACUMULACION{Style.RESET_ALL}"
        icon = "🟢"
        sc = Fore.GREEN
    elif score >= -0.40:
        signal = f"{Fore.YELLOW}NEUTRAL{Style.RESET_ALL}"
        icon = "🟡"
        sc = Fore.YELLOW
    else:
        signal = f"{Fore.RED}DISTRIBUCION{Style.RESET_ALL}"
        icon = "🔴"
        sc = Fore.RED

    dim = Fore.WHITE + Style.DIM
    rst = Style.RESET_ALL
    bld = Style.BRIGHT

    print()
    print(f"  {Fore.YELLOW}{bld}MGI{rst}  {dim}Marcos Gainza Index{rst}")
    print(f"  {dim}{'═' * 46}{rst}")
    print()
    print(f"  MGI SCORE:  {sc}{bld}{fmt(score)}{rst}   {icon} {signal}")
    print()
    print(f"  {dim}{'─' * 46}{rst}")
    print(f"  {Fore.YELLOW}I₁{rst}  Posición seno     {color_score(mgi['I1'])}{fmt(mgi['I1'])}{rst}    {dim}-sin(ωt+φ){rst}")
    print(f"  {Fore.YELLOW}I₂{rst}  Pendiente         {color_score(mgi['I2'])}{fmt(mgi['I2'])}{rst}    {dim}sign(Δy){rst}")
    print(f"  {Fore.YELLOW}I₃{rst}  Dist. a media     {color_score(mgi['I3'])}{fmt(mgi['I3'])}{rst}    {dim}-(y-μ)/A{rst}")
    print(f"  {Fore.YELLOW}I₄{rst}  Confiabilidad ε   {color_conf(mgi['I4'])}{mgi['I4']:.2f}{rst}     {dim}1-(σ/25){rst}")
    print(f"  {Fore.YELLOW}I₅{rst}  Cruce medias      {color_score(mgi['I5'])}{fmt(mgi['I5'])}{rst}    {dim}sign(MA₅-MA₂₀){rst}")
    print(f"  {dim}{'─' * 46}{rst}")
    print()

    # Market data
    btc_c = Fore.GREEN if (DATA["btc_chg"] or 0) >= 0 else Fore.RED
    btc_arr = "▲" if (DATA["btc_chg"] or 0) >= 0 else "▼"
    btc_str = f"${DATA['btc']:,.0f}  {btc_arr} {abs(DATA['btc_chg'] or 0):.2f}%" if DATA["btc"] else "..."
    print(f"  {dim}BTC{rst}         {btc_c}{btc_str}{rst}")

    inf_c = Fore.RED if (DATA["inflation"] or 0) > 4 else Fore.YELLOW
    print(f"  {dim}INF USA{rst}     {inf_c}{DATA['inflation'] or '...'}% anual{rst}")

    fed_c = Fore.RED if (DATA["fed_rate"] or 0) > 4 else Fore.YELLOW
    print(f"  {dim}TASA FED{rst}    {fed_c}{DATA['fed_rate'] or '...'}%{rst}")

    print(f"  {dim}BLUE ARS{rst}    {Fore.YELLOW}${DATA['blue'] or '...'}{rst}")
    print(f"  {dim}OFICIAL{rst}     {Fore.YELLOW}${DATA['oficial'] or '...'}{rst}")
    print(f"  {dim}BRECHA{rst}      {Fore.YELLOW}{DATA['brecha'] or '...'}%{rst}")

    rp = DATA["riesgo_pais"]
    rp_c = Fore.RED if (rp or 0) > 800 else (Fore.YELLOW if (rp or 0) > 400 else Fore.GREEN)
    print(f"  {dim}RIESGO PAIS{rst} {rp_c}{rp or '...'} pts{rst}")

    print()
    print(f"  {dim}{'─' * 46}{rst}")
    print(f"  {dim}A={rst}{P['A']:.1f}  {dim}ω={rst}{P['omega']:.2f}  {dim}φ={rst}{P['phi']:.2f}  {dim}b={rst}{P['b']:.2f}  {dim}σ={rst}{P['sigma']:.1f}  {dim}δ={rst}{P['delta']:.3f}")
    print(f"  {dim}y(t) = {rst}{fmt(mgi['yt'])}")
    print(f"  {dim}MGI = [(I₁+I₂+I₃+I₅)/4] × conf(ε){rst}")
    print()
    print(f"  {dim}Actualizado: {datetime.now().strftime('%H:%M:%S')}  ·  Próximo en {INTERVAL}s{rst}")
    print(f"  {dim}Ctrl+C para salir{rst}")
    print()


# ════════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════════
def main():
    global t

    print(f"\n  {Fore.YELLOW}{Style.BRIGHT}MGI{Style.RESET_ALL}  Iniciando...")
    print(f"  Conectando a APIs...\n")

    fetch_all()

    # Pre-fill history
    for i in range(25):
        yt = y_clean(t - (25 - i) * 0.1)
        history.append(yt)

    try:
        while True:
            t += 0.003 * INTERVAL
            mgi = calc_mgi()
            render(mgi)

            # Wait and refetch
            time.sleep(INTERVAL)
            fetch_all()

    except KeyboardInterrupt:
        print(f"\n  {Fore.YELLOW}MGI finalizado.{Style.RESET_ALL}\n")


if __name__ == "__main__":
    main()
