# -*- coding: utf-8 -*-
"""
Meteo Cantiere Torino — controllo automatico pioggia intensa e grandine.
Interroga Open-Meteo (nowcast), valuta i prossimi 60 minuti su Torino
e invia un messaggio Telegram in caso di ATTENZIONE o ALLERTA.
Nessuna modifica necessaria: la soglia si regola dal file meteo.yml.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

LAT, LON = 45.0703, 7.6869  # Torino centro
SOGLIA_ALLERTA = float(os.environ.get("SOGLIA_MMH", "10"))   # mm/h pioggia molto forte
SOGLIA_ATTENZIONE = SOGLIA_ALLERTA * 0.4
RINOTIFICA_MIN = 45          # se resta ALLERTA, nuovo messaggio ogni 45 minuti
STATE_FILE = "state.json"

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&minutely_15=precipitation,weather_code&hourly=cape"
    "&forecast_minutely_15=8&forecast_hours=3&timezone=Europe%2FRome"
)

CODICI = {95: "temporale", 96: "temporale con grandine", 99: "temporale con grandine forte"}


def invia_telegram(testo: str) -> None:
    dati = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": testo}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=dati
    )
    urllib.request.urlopen(req, timeout=30)


def carica_stato() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"livello": 0, "ultimo_avviso": 0}


def salva_stato(stato: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(stato, f)


def valuta(dati: dict) -> dict:
    """Restituisce livello (0/1/2), motivi, orario primo fenomeno, mm/h max."""
    offset = dati.get("utc_offset_seconds", 0)
    adesso_locale = time.time() + offset  # epoca "spostata" nell'ora locale di Torino

    m = dati["minutely_15"]
    celle = [
        {
            "iso": t,
            "mmh": (m["precipitation"][i] or 0) * 4,
            "codice": m["weather_code"][i] or 0,
        }
        for i, t in enumerate(m["time"])
    ]

    # Ora locale di Torino in formato ISO (i tempi Open-Meteo sono già locali)
    ora_iso = datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")

    # Primi 4 intervalli da 15 minuti non ancora conclusi (= prossimi 60 minuti)
    futuri = []
    for c in celle:
        fine = datetime.fromisoformat(c["iso"]) + timedelta(minutes=15)
        if fine.strftime("%Y-%m-%dT%H:%M") > ora_iso:
            futuri.append(c)
        if len(futuri) == 4:
            break

    cape = 0.0
    for v in (dati.get("hourly", {}).get("cape") or []):
        cape = max(cape, v or 0)

    livello = 0
    motivi = []
    primo = None
    mmh_max = 0.0
    codice_peggiore = 0

    for c in futuri:
        mmh_max = max(mmh_max, c["mmh"])
        if c["codice"] in (96, 99):
            codice_peggiore = max(codice_peggiore, c["codice"])
        lvl = 0
        if c["mmh"] >= SOGLIA_ALLERTA or c["codice"] in (96, 99):
            lvl = 2
        elif c["mmh"] >= SOGLIA_ATTENZIONE or c["codice"] == 95:
            lvl = 1
            if codice_peggiore == 0 and c["codice"] == 95:
                codice_peggiore = 95
        if lvl > 0 and primo is None:
            primo = c["iso"][11:16]
        livello = max(livello, lvl)

    if codice_peggiore in CODICI:
        motivi.append(CODICI[codice_peggiore] + (" previsto" if codice_peggiore == 95 else " prevista"))
    if mmh_max >= SOGLIA_ALLERTA:
        motivi.insert(0, f"pioggia molto forte ({mmh_max:.1f} mm/h)")
    elif mmh_max >= SOGLIA_ATTENZIONE:
        motivi.append(f"pioggia in intensificazione ({mmh_max:.1f} mm/h)")
    if cape >= 1500 and mmh_max > 0.5:
        if livello < 1:
            livello = 1
        motivi.append(f"condizioni favorevoli a grandine (CAPE {cape:.0f} J/kg)")

    return {"livello": livello, "motivi": motivi, "primo": primo, "mmh_max": mmh_max}


def main() -> None:
    if not TOKEN or not CHAT_ID:
        print("ERRORE: mancano i secret TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
        sys.exit(1)

    if os.environ.get("TEST_MESSAGE") == "1":
        invia_telegram(
            "✅ Prova riuscita — Meteo Cantiere Torino è attivo.\n"
            "Da ora controllo il meteo ogni 15 minuti e ti avviso in caso di "
            "pioggia intensa o grandine prevista nella prossima ora."
        )
        print("Messaggio di prova inviato.")
        return

    try:
        with urllib.request.urlopen(API_URL, timeout=30) as r:
            dati = json.load(r)
    except Exception as e:
        print(f"Dati meteo non disponibili ({e}); riproverò al prossimo giro.")
        return

    esito = valuta(dati)
    stato = carica_stato()
    prima = stato.get("livello", 0)
    livello = esito["livello"]
    adesso = time.time()
    quando = f" dalle {esito['primo']}" if esito["primo"] else ""
    dettaglio = ", ".join(esito["motivi"]) if esito["motivi"] else "fenomeni intensi"

    if livello == 2 and (prima < 2 or adesso - stato.get("ultimo_avviso", 0) > RINOTIFICA_MIN * 60):
        invia_telegram(
            f"🔴 ALLERTA METEO — Torino\n{dettaglio}{quando} (prossimi 60 minuti).\n"
            "Valuta la messa in sicurezza del cantiere."
        )
        stato["ultimo_avviso"] = adesso
    elif livello == 1 and prima < 1:
        invia_telegram(
            f"🟡 Attenzione meteo — Torino\n{dettaglio}{quando} (prossimi 60 minuti).\n"
            "Situazione da tenere d'occhio."
        )
        stato["ultimo_avviso"] = adesso
    elif livello == 0 and prima == 2:
        invia_telegram(
            "🟢 Rientro allerta — Torino\n"
            "Nessuna pioggia intensa o grandine prevista nella prossima ora."
        )

    stato["livello"] = livello
    salva_stato(stato)
    print(f"Livello {livello} — max {esito['mmh_max']:.1f} mm/h — {dettaglio}")


if __name__ == "__main__":
    main()
