#!/usr/bin/env python3
"""
fetch_rates.py - dnevni dohvat tečajeva za travel currency-converter PWA.

Proizvodi JEDNU datoteku rates.json:
  * po državi (HR, VN): središnja banka + najveće banke (kupovni / srednji / prodajni,
    odvojeno gotovina (cash/efektiva) i devize (transfer/bezgotovinski))
  * global_reference: ECB + open.er-api.com + fawazahmed0 currency-api (mid tečajevi
    za sve svjetske valute, baza EUR) -> svaka država ima barem referentni tečaj

Konvencija u countries.*.sources[].rates:
  vrijednost = koliko LOKALNE valute države vrijedi 1 jedinica strane valute
  (HR: EUR za 1 USD;  VN: VND za 1 EUR).  buy = banka KUPUJE stranu valutu od
  klijenta, sell = banka PRODAJE stranu valutu klijentu. Uvijek buy <= middle <= sell.
  HR banke objavljuju obrnuto ("1 EUR = x strane valute"); skripta to invertira, a
  originalni zapis je u polju "published" svake HR stavke (ako je include_published).

Svaki adapter je izoliran: greška jedne banke -> status "error" + poruka, ostalo radi.
Ovisnosti: Python 3.9+, requests.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

UA = os.environ.get(
    "FX_USER_AGENT",
    "Mozilla/5.0 (compatible; TravelFXFetcher/1.0; personal currency app, 1 request/day)",
)
TIMEOUT = 25
TZ_HR = ZoneInfo("Europe/Zagreb")
TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "hr,en;q=0.8,vi;q=0.6"})


# ----------------------------------------------------------------- helpers
def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def get(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    r = SESSION.get(url, **kw)
    r.raise_for_status()
    return r


def num(s, fmt="dot"):
    """Parse number string. fmt: 'dot' = 1,234.56 ; 'comma' = 1.234,56 (HR/VN)."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        return v if v > 0 else None
    s = htmllib.unescape(str(s)).replace("\xa0", " ").strip()
    s = re.sub(r"[^\d.,\-]", "", s)
    if not s or s in ("-", "0"):
        return None
    if fmt == "comma":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def r8(x):
    return None if x is None else float(f"{x:.8g}")


def inv(x):
    return None if not x else r8(1.0 / x)


def mk_row(unit=1, middle=None, transfer_buy=None, transfer_sell=None,
           cash_buy=None, cash_sell=None, published=None):
    """Row in normalized convention (local currency per 1 unit foreign)."""
    u = unit or 1
    f = lambda v: None if v is None else r8(v / u)
    row = {
        "middle": f(middle),
        "transfer": {"buy": f(transfer_buy), "sell": f(transfer_sell)},
        "cash": {"buy": f(cash_buy), "sell": f(cash_sell)},
    }
    if published:
        row["published"] = published
    return row


def hr_row(unit, kup_dev, srednji, prod_dev, kup_ef, prod_ef, keep_published=True):
    """HR banks publish 'foreign units per 1 EUR'. Invert to 'EUR per 1 foreign'.
    HR kupovni (bank buys foreign) is the LARGER number of foreign per EUR,
    so its inverse is the smaller EUR value -> buy. Consistent."""
    u = unit or 1
    # published numbers are per `u` EUR?  No: HR lists post-2023 are per 1 EUR, unit=1.
    pub = None
    if keep_published:
        pub = {"quote": "foreign_per_1_EUR", "kupovni_devize": kup_dev, "srednji": srednji,
               "prodajni_devize": prod_dev, "kupovni_efektiva": kup_ef, "prodajni_efektiva": prod_ef}
    return mk_row(1, inv(srednji), inv(kup_dev), inv(prod_dev), inv(kup_ef), inv(prod_ef), pub)


def source(id_, name, kind, url, page_url, fn, enabled=True, disabled_reason=None):
    return dict(id=id_, name=name, kind=kind, url=url, page_url=page_url, fn=fn,
                enabled=enabled, disabled_reason=disabled_reason)


# ======================================================= CROATIA (HR, EUR)
def hnb():
    url = "https://api.hnb.hr/tecajn-eur/v3"
    data = get(url).json()
    rates, valid = {}, None
    for x in data:
        valid = x.get("datum_primjene")
        rates[x["valuta"]] = hr_row(1, num(x["kupovni_tecaj"], "comma"), num(x["srednji_tecaj"], "comma"),
                                    num(x["prodajni_tecaj"], "comma"), None, None)
        # HNB: no cash rates; its buy/sell are "devizni" (transfer) only
    return {"valid_for": valid, "list_number": data[0].get("broj_tecajnice") if data else None,
            "middle_source": "published", "rates": rates}


def pbz():
    url = ("https://www.pbz.hr/digitalServicesServlet/?operation=getExchangeRates&httpMethod=GET"
           "&endpointName=getExchangeRates&listType=PBZ&headers=standardHeaders&bank=PBZ&bankId=PBZ"
           "&locale=hr&applicationId=PublicPortal&refCurrency=EUR")
    d = get(url, headers={"Accept": "application/json"}).json()
    if not d or not d.get("rates"):
        raise RuntimeError(f"empty response: {str(d)[:200]}")
    g = lambda x, k: (x.get(k) or {}).get("rate")
    rates, valid = {}, None
    for x in d["rates"]:
        cur = x["toCurrency"]["label"]
        valid = (x.get("validFromDate") or "")[:10] or valid
        unit = (x.get("meanRate") or {}).get("units") or 1
        rates[cur] = hr_row(unit, num(g(x, "buyRateCurrency")), num(g(x, "meanRate")),
                            num(g(x, "sellingRateCurrency")), num(g(x, "buyRateCash")),
                            num(g(x, "sellingRateCash")))
    return {"valid_for": valid, "middle_source": "published", "rates": rates}


def zaba():
    page = "https://www.zaba.hr/home/tecajna"
    h = get(page).text
    m = re.search(r'/home/tecajnaDok/txt/(\d{4}-\d{2}-\d{2})', h)
    day = m.group(1) if m else dt.datetime.now(TZ_HR).date().isoformat()
    url = f"https://www.zaba.hr/home/tecajnaDok/txt/{day}"
    txt = get(url).content.decode("utf-8", errors="replace")
    rates = {}
    for line in txt.splitlines():
        mm = re.match(r"^(\d{3})\s+([A-Z]{3})\s+(\d+)", line)
        if not mm:
            continue
        cols = [line[21 + 18 * i: 39 + 18 * i].strip() for i in range(5)]
        ke, kd, sr, pd, pe = [num(c) for c in cols]
        rates[mm.group(2)] = hr_row(int(mm.group(3)), kd, sr, pd, ke, pe)
    if not rates:
        raise RuntimeError("no rows parsed from TXT")
    return {"valid_for": day, "url": url, "middle_source": "published", "rates": rates}


def otp():
    today = dt.datetime.now(TZ_HR).strftime("%d.%m.%Y.")
    url = f"https://www.otpbanka.hr/apps/elementa/getExchangeRates?date={today}"
    hdr = {"Accept": "application/json"}
    if os.environ.get("FX_OTP_BROWSER_UA") == "1":
        # OTP's WAF returns 403 for any non-browser User-Agent (robots.txt itself allows /apps/).
        # Opt-in only: decide yourself whether this is acceptable for your use.
        hdr["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
    r = SESSION.get(url, headers=hdr, timeout=TIMEOUT)
    if r.status_code == 403:
        raise RuntimeError("403 Forbidden – OTP WAF blokira ne-browser User-Agent (opt-in: FX_OTP_BROWSER_UA=1)")
    r.raise_for_status()
    d = r.json()
    err = (d.get("errors") or [{}])[0]
    if str(err.get("code")) != "0":
        raise RuntimeError(f"OTP error: {err}")
    er = d["exchangeRates"]
    rates = {}
    for x in er["rates"]:
        rates[x["currency"]] = hr_row(int(x.get("unit") or 1), num(x.get("foreignCurrencyBuy"), "comma"),
                                      num(x.get("cashMiddle"), "comma"), num(x.get("foreignCurrencySell"), "comma"),
                                      num(x.get("cashBuy"), "comma"), num(x.get("cashSell"), "comma"))
    ed = er.get("listEffectiveDate")  # dd.mm.yyyy
    valid = dt.datetime.strptime(ed, "%d.%m.%Y").date().isoformat() if ed else None
    return {"valid_for": valid, "list_number": re.sub(r"\D", "", er.get("listNumber") or "") or None,
            "url": url, "middle_source": "published", "rates": rates}


RBA_DEFAULT_LIST = "USD-CHF-GBP-AUD-JPY-BAM-CAD-CZK-DKK-HUF-NOK-NZD-PLN-RSD-SEK-TRY"


def rba():
    page = "https://www.rba.hr/hr/tecajna-lista.html"
    curlist = RBA_DEFAULT_LIST
    try:
        h = htmllib.unescape(get(page).text)
        m = re.search(r'/hr\.exchangerates\.\{0\}\.BASE\.([A-Z\-]+)\.EUR\.TARGET\.json', h)
        if m:
            curlist = m.group(1)
    except Exception:
        pass
    day = dt.datetime.now(TZ_HR).strftime("%Y%m%d")
    url = f"https://www.rba.hr/hr.exchangerates.{day}.BASE.{curlist}.EUR.TARGET.json"
    d = get(url, headers={"Accept": "application/json"}).json()
    rates, valid, lst = {}, None, None
    for block in d.get("rates", []):
        for x in block.get("currencyList", []):
            cur = x["currencyPair"]["left"]["code"]
            v = lambda k: num((x.get(k) or {}).get("value"))
            rates[cur] = hr_row(x.get("unit") or 1, v("buyRate"), v("middleRate"), v("sellRate"),
                                v("buyEffectiveRate"), v("sellEffectiveRate"))
            lst = x.get("listNumber", lst)
            if x.get("validityDate"):
                try:
                    valid = dt.datetime.strptime(x["validityDate"].split(",")[0] + "," + x["validityDate"].split(",")[1],
                                                 "%b %d, %Y").date().isoformat()
                except Exception:
                    valid = x["validityDate"]
    if not rates:
        raise RuntimeError("no rates in JSON")
    return {"valid_for": valid, "list_number": lst, "url": url, "middle_source": "published", "rates": rates}


def addiko():
    url = "https://www.addiko.hr/gradanstvo/izracuni/tecajna-lista/"
    h = get(url).text
    t = re.search(r'<table id="exchange-list">(.*?)</table>', h, re.S)
    if not t:
        raise RuntimeError("table #exchange-list not found")
    rates = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", t.group(1), re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 9 or not re.fullmatch(r"[A-Z]{3}", cells[2]):
            continue
        kd, sr, pd, ke, pe = [num(c, "comma") for c in cells[4:9]]
        rates[cells[2]] = hr_row(int(cells[3] or 1), kd, sr, pd, ke, pe)
    m = re.search(r"Primjenjuje se od:\s*<strong>([\d.]+)</strong>", h)
    valid = dt.datetime.strptime(m.group(1).strip("."), "%d.%m.%Y").date().isoformat() if m else None
    return {"valid_for": valid, "middle_source": "published", "rates": rates}


def erste():
    url = "https://local.erstebank.hr/api/v1/fx"
    d = get(url, headers={"Accept": "application/json", "Origin": "https://www.erstebank.hr"}).json()
    rates = {}
    for x in d.get("currencies", []):
        r = x.get("rates", {})
        rates[x["name"]] = hr_row(x.get("unit") or 1, num(r.get("buying")), num(r.get("middle")),
                                  num(r.get("selling")), num(r.get("buyingCash")), num(r.get("sellingCash")))
    valid = None
    if d.get("date"):
        valid = dt.datetime.fromisoformat(d["date"].replace("Z", "+00:00")).astimezone(TZ_HR).date().isoformat()
    return {"valid_for": valid, "list_number": d.get("number"), "middle_source": "published", "rates": rates}


# ======================================================= VIETNAM (VN, VND)
def vn_row(cash_buy, transfer_buy, sell, cash_sell=None, transfer_sell=None):
    ts = transfer_sell if transfer_sell is not None else sell
    cs = cash_sell if cash_sell is not None else sell
    mid = r8((transfer_buy + ts) / 2) if transfer_buy and ts else None
    return mk_row(1, mid, transfer_buy, ts, cash_buy, cs)


def tcb_fetch():
    url = "https://techcombank.com/content/techcombank/web/vn/vi/cong-cu-tien-ich/ty-gia/_jcr_content.exchange-rates.integration.json"
    return url, get(url, headers={"Accept": "application/json"}).json()


_TCB_CACHE = {}


import threading
_LOCK = threading.Lock()


def tcb_data():
    with _LOCK:
        if "d" not in _TCB_CACHE:
            _TCB_CACHE["d"] = tcb_fetch()
        return _TCB_CACHE["d"]


def sbv_central_via_tcb():
    url, d = tcb_data()
    o = (d.get("otherRate") or {}).get("data") or []
    if not o:
        raise RuntimeError("no otherRate in Techcombank JSON")
    c = o[0]
    central = num(c.get("central"))
    return {"url": url, "note": "Tỷ giá trung tâm NHNN (SBV central rate USD/VND, band ±5%) kako ga objavljuje Techcombank; SBV ne objavljuje ostale valute javno dostupnim API-jem.",
            "middle_source": "published",
            "rates": {"USD": {"middle": central, "transfer": {"buy": None, "sell": None},
                               "cash": {"buy": None, "sell": None},
                               "floor": num(c.get("floor")), "ceiling": num(c.get("ceiling"))}}}


def vietcombank():
    day = dt.datetime.now(TZ_VN).date().isoformat()
    url = f"https://www.vietcombank.com.vn/api/exchangerates?date={day}"
    try:
        d = get(url, headers={"Accept": "application/json"}).json()
        rates = {x["currencyCode"]: vn_row(num(x.get("cash")), num(x.get("transfer")), num(x.get("sell")))
                 for x in d.get("Data", [])}
        if rates:
            return {"valid_for": (d.get("Date") or "")[:10], "published_at": d.get("UpdatedDate"),
                    "url": url, "middle_source": "computed_avg_transfer", "rates": rates}
    except Exception:
        pass
    # fallback: official XML feed (max 1 request / 5 min per their note)
    url = "https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx?b=10"
    root = ET.fromstring(get(url).content)
    rates = {}
    for e in root.iter("Exrate"):
        rates[e.get("CurrencyCode").strip()] = vn_row(num(e.get("Buy")), num(e.get("Transfer")), num(e.get("Sell")))
    return {"published_at": (root.findtext("DateTime") or "").strip(), "url": url,
            "middle_source": "computed_avg_transfer", "rates": rates}


def bidv():
    url = "https://bidv.com.vn/ServicesBIDV/ExchangeDetailServlet"
    d = get(url, headers={"Accept": "application/json", "Referer": "https://bidv.com.vn/vn/ty-gia-ngoai-te"}).json()
    rates = {}
    for x in d.get("data", []):
        cur = x.get("currency", "")
        if not re.fullmatch(r"[A-Z]{3}", cur):   # skip USD(1-2-5), USD(10-20) small-note rows
            continue
        rates[cur] = vn_row(num(x.get("muaTm")), num(x.get("muaCk")), num(x.get("ban")))
    if not rates:
        raise RuntimeError("no rates")
    pub = None
    if d.get("day_vi"):
        pub = dt.datetime.strptime(f'{d["day_vi"]} {d.get("hour","00:00")}', "%d/%m/%Y %H:%M").replace(tzinfo=TZ_VN).isoformat()
    return {"published_at": pub, "middle_source": "computed_avg_transfer", "rates": rates}


def vietinbank():
    url = "https://www.vietinbank.vn/ca-nhan/ty-gia-khcn"
    h = get(url).text
    i = h.find("Chuyển khoản</p>")
    if i < 0:
        raise RuntimeError("rate table not found")
    tb = h[h.rfind("<table", 0, i): h.find("</table>", i)]
    rates = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
        cells = [htmllib.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) >= 4 and re.fullmatch(r"[A-Z]{3}", cells[0]):
            # '#' = big notes (50/100) for USD/EUR; '&' small notes row is skipped (no currency code)
            rates[cells[0]] = vn_row(num(cells[1], "comma"), num(cells[2], "comma"), num(cells[3], "comma"))
    m = re.search(r"cập nhật lúc\s*(?:<[^>]+>\s*)*([\d:]{5,8})\s*(?:<[^>]+>\s*)*ngày\s*(?:<[^>]+>\s*)*(\d{2}/\d{2}/\d{4})",
                  h)
    pub = None
    if m:
        t = m.group(1) if len(m.group(1)) == 8 else m.group(1) + ":00"
        pub = dt.datetime.strptime(f"{m.group(2)} {t}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=TZ_VN).isoformat()
    if not rates:
        raise RuntimeError("no rows parsed")
    return {"published_at": pub, "middle_source": "computed_avg_transfer", "rates": rates}


def techcombank():
    url, d = tcb_data()
    rates = {}
    for x in (d.get("exchangeRate") or {}).get("data", []):
        lab = x.get("label", "")
        cur = x.get("sourceCurrency")
        if lab != cur and not lab.startswith("USD (50"):   # USD: keep only (50,100) big notes row
            continue
        rates[cur] = vn_row(num(x.get("bidRateTM")), num(x.get("bidRateCK")), num(x.get("askRate")),
                            cash_sell=num(x.get("askRateTM")), transfer_sell=num(x.get("askRate")))
    pub = None
    ds = [x.get("inputDate") for x in (d.get("exchangeRate") or {}).get("data", []) if x.get("inputDate")]
    if ds:
        pub = dt.datetime.fromisoformat(max(ds).replace("Z", "+00:00")).astimezone(TZ_VN).isoformat()
    return {"published_at": pub, "url": url, "middle_source": "computed_avg_transfer", "rates": rates}


def agribank():
    url = "https://www.agribank.com.vn/vn/ty-gia"
    h = get(url).text
    i = h.find('id="tyGiaCn"')
    if i < 0:
        raise RuntimeError("#tyGiaCn not found")
    blk = h[i: h.find("</table>", i)]
    rates = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", blk, re.S):
        cells = [htmllib.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) >= 4 and re.fullmatch(r"[A-Z]{3}", cells[0]):
            rates[cells[0]] = vn_row(num(cells[1]), num(cells[2]), num(cells[3]))
    m = re.search(r"cập nhật lúc\s*([\d:]+)\s*ngày\s*(\d{2}/\d{2}/\d{4})", blk)
    pub = dt.datetime.strptime(f"{m.group(2)} {m.group(1)}", "%d/%m/%Y %H:%M").replace(tzinfo=TZ_VN).isoformat() if m else None
    if not rates:
        raise RuntimeError("no rows parsed")
    return {"published_at": pub, "middle_source": "computed_avg_transfer", "rates": rates}


def acb():
    t = dt.datetime.now(TZ_VN).strftime("%Y-%m-%dT%H:%M:%S")
    url = f"https://acb.com.vn/api/front/v1/currency?currency=VND&effectiveDateTime={t}"
    d = get(url, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}).json()
    best = {}  # (cur, deal, instr) -> (denomination, rate)
    for x in d:
        cur = x.get("exchangeCurrency", "")
        if not re.fullmatch(r"[A-Z]{3}", cur) or cur in ("ACB", "XAU"):
            continue
        key = (cur, x.get("dealType"), x.get("instrumentType"))
        den = x.get("denomination") or 99
        if den == 99 or key not in best or den > best[key][0]:
            best[key] = (den, x.get("exchangeRate"))
    curs = sorted({k[0] for k in best})
    g = lambda c, d_, i: num(best.get((c, d_, i), (0, None))[1])
    rates = {c: vn_row(g(c, "BID", "CASH"), g(c, "BID", "TRANSFER"), g(c, "ASK", "TRANSFER"),
                       cash_sell=g(c, "ASK", "CASH"), transfer_sell=g(c, "ASK", "TRANSFER")) for c in curs}
    if not rates:
        raise RuntimeError("no rates")
    return {"url": "https://acb.com.vn/api/front/v1/currency?currency=VND&effectiveDateTime=<now VN>",
            "middle_source": "computed_avg_transfer", "rates": rates}


# ======================================================= GLOBAL REFERENCE
def ecb():
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
    root = ET.fromstring(get(url).content)
    ns = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    day = root.find(".//e:Cube[@time]", ns)
    rates = {c.get("currency"): float(c.get("rate")) for c in day.findall("e:Cube", ns)}
    rates["EUR"] = 1.0
    return {"valid_for": day.get("time"), "rates_per_eur": rates}


def open_er():
    url = "https://open.er-api.com/v6/latest/EUR"
    d = get(url).json()
    if d.get("result") != "success":
        raise RuntimeError(str(d)[:200])
    return {"valid_for": dt.datetime.fromtimestamp(d["time_last_update_unix"], dt.timezone.utc).date().isoformat(),
            "published_at": dt.datetime.fromtimestamp(d["time_last_update_unix"], dt.timezone.utc).isoformat(),
            "attribution": "Rates By Exchange Rate API (https://www.exchangerate-api.com)",
            "rates_per_eur": d["rates"]}


def fawaz(iso_codes):
    urls = ["https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/eur.json",
            "https://latest.currency-api.pages.dev/v1/currencies/eur.json"]
    last = None
    for url in urls:
        try:
            d = get(url).json()
            rates = {k.upper(): v for k, v in d["eur"].items()
                     if len(k) == 3 and k.isalpha() and (not iso_codes or k.upper() in iso_codes) and v}
            return {"valid_for": d.get("date"), "url": url, "rates_per_eur": rates}
        except Exception as e:  # try mirror
            last = e
    raise last


# ======================================================= WORLD (added countries)
def _cells(tr):
    return [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]


def _rows(html_text):
    return [_cells(tr) for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, re.S | re.I)]


def lrow(unit=1, middle=None, tb=None, ts=None, cb=None, cs=None, compute_mid=True):
    """Row from 'local per `unit` foreign' numbers; middle computed from transfer (or cash) if missing."""
    middle, tb, ts, cb, cs = [num(v) if isinstance(v, (int, float)) else v for v in (middle, tb, ts, cb, cs)]
    if middle is None and compute_mid:
        if tb and ts:
            middle = (tb + ts) / 2
        elif cb and cs:
            middle = (cb + cs) / 2
    return mk_row(unit, middle, tb, ts, cb, cs)


_ECB_CACHE = {}


def ecb_cached():
    with _LOCK:
        if "d" not in _ECB_CACHE:
            _ECB_CACHE["d"] = ecb()
        return _ECB_CACHE["d"]


def ecb_as_central(note=None):
    def fn():
        d = ecb_cached()
        rates = {c: mk_row(1, inv(v)) for c, v in d["rates_per_eur"].items() if c != "EUR"}
        out = {"valid_for": d["valid_for"], "middle_source": "published", "rates": rates}
        if note:
            out["note"] = note
        return out
    return fn


# --- Poland: NBP
def nbp():
    a = get("https://api.nbp.pl/api/exchangerates/tables/A/?format=json").json()[0]
    c = get("https://api.nbp.pl/api/exchangerates/tables/C/?format=json").json()[0]
    bidask = {x["code"]: x for x in c["rates"]}
    rates = {}
    for x in a["rates"]:
        ba = bidask.get(x["code"], {})
        rates[x["code"]] = lrow(1, x["mid"], ba.get("bid"), ba.get("ask"), compute_mid=False)
    return {"valid_for": a["effectiveDate"], "list_number": a["no"], "middle_source": "published",
            "note": "mid = tabela A; buy/sell = tabela C (NBP kupno/sprzedaż)", "rates": rates}


# --- Czechia: CNB + Komerční banka
def cnb():
    url = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
    lines = get(url).text.strip().splitlines()
    day = dt.datetime.strptime(lines[0].split()[0], "%d.%m.%Y").date().isoformat()
    rates = {}
    for l in lines[2:]:
        p = l.split("|")
        if len(p) == 5:
            rates[p[3]] = lrow(int(p[2]), num(p[4], "comma"), compute_mid=False)
    return {"valid_for": day, "middle_source": "published", "rates": rates}


def kb_cz():
    url = "https://www.kb.cz/cs/kurzovni-listek"
    h = get(url).text
    i = h.find('"api": "/api/exchangerate/list"')
    if i < 0:
        raise RuntimeError("embedded KB exchange-rate JSON not found")
    j = h.find('"items":', i)
    arr, _ = json.JSONDecoder().raw_decode(h[j + len('"items":'):].lstrip())
    rates = {}
    for x in arr:
        cur = x.get("curencyIso")
        if not cur:
            continue
        rates[cur] = lrow(x.get("currencyUnit") or 1, num(x.get("middleCNB")), num(x.get("nonCashBuy")),
                          num(x.get("nonCashSell")), num(x.get("cashBuy")), num(x.get("cashSell")))
    q = re.search(r'"queryDate":\s*"([\d-]{10})', h[i:i + 500])
    return {"valid_for": q.group(1) if q else None, "middle_source": "published (ČNB middle)", "rates": rates}


# --- Hungary: MNB SOAP (plain HTTP endpoint)
def mnb():
    url = "http://www.mnb.hu/arfolyamok.asmx"
    body = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body><GetCurrentExchangeRates xmlns="http://www.mnb.hu/webservices/"/></soap:Body></soap:Envelope>')
    r = SESSION.post(url, data=body.encode(), timeout=TIMEOUT, headers={
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": '"http://www.mnb.hu/webservices/MNBArfolyamServiceSoap/GetCurrentExchangeRates"'})
    r.raise_for_status()
    inner = re.search(r"<GetCurrentExchangeRatesResult>(.*?)</GetCurrentExchangeRatesResult>", r.text, re.S).group(1)
    root = ET.fromstring(htmllib.unescape(inner))
    day = root.find("Day")
    rates = {x.get("curr"): lrow(int(x.get("unit") or 1), num(x.text, "comma"), compute_mid=False)
             for x in day.findall("Rate")}
    return {"valid_for": day.get("date"), "middle_source": "published", "rates": rates}


# --- Sweden / Denmark
def riksbank():
    d = get("https://api.riksbank.se/swea/v1/Observations/Latest/ByGroup/130").json()
    latest = max(x["date"] for x in d)
    rates = {x["seriesId"][3:6]: lrow(1, float(x["value"]), compute_mid=False)
             for x in d if x["date"] == latest and x.get("value")}
    return {"valid_for": latest, "middle_source": "published", "rates": rates}


def nationalbanken():
    root = ET.fromstring(get("https://www.nationalbanken.dk/api/currencyratesxml?lang=en").content)
    day = root.find("dailyrates")
    rates = {c.get("code"): lrow(100, num(c.get("rate")), compute_mid=False) for c in day.findall("currency")}
    return {"valid_for": day.get("id"), "middle_source": "published", "rates": rates}


# --- Slovenia: NLB (EUR based, foreign per 1 EUR)
def nlb_si():
    url = "https://www.nlb.si/services/tecajnica/?type=individuals&format=json"
    rates, day = {}, None
    for l in get(url).text.splitlines():
        p = l.split()
        if len(p) >= 7 and re.fullmatch(r"[A-Z]{3}", p[5]):
            day = p[1]
            buy, sell = num(p[6].lstrip("0") or "0", "comma"), num(p[7].lstrip("0") if len(p) > 7 else None, "comma")
            rates[p[5]] = hr_row(1, buy, None, sell, None, None, keep_published=False)
            mid = rates[p[5]]["transfer"]
            if mid["buy"] and mid["sell"]:
                rates[p[5]]["middle"] = r8((mid["buy"] + mid["sell"]) / 2)
    if not rates:
        raise RuntimeError("no rows")
    return {"valid_for": f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else None,
            "middle_source": "computed_avg_transfer", "note": "NLB tečajnica za fizične osebe (devizni)", "rates": rates}


# --- Serbia: NBS official list via kurs.resenje.org mirror (nbs.rs refuses non-browser connections)
def nbs_rs():
    url = "https://kurs.resenje.org/api/v1/rates/today"
    d = get(url).json()
    rates, day = {}, None
    for x in d["rates"]:
        day = x.get("date_from") or day
        rates[x["code"]] = lrow(x.get("parity") or 1, x.get("exchange_middle"), x.get("exchange_buy"),
                                x.get("exchange_sell"), x.get("cash_buy"), x.get("cash_sell"), compute_mid=False)
    return {"valid_for": day, "middle_source": "published",
            "note": "Službena kursna lista NBS (devizni + efektivni), preuzeto preko kurs.resenje.org API-ja", "rates": rates}


# --- Bosnia: CBBH
def cbbh():
    day = dt.datetime.now(TZ_HR).strftime("%d.%m.%Y")
    url = f"https://www.cbbh.ba/CurrencyExchange/GetJson?date={day}"
    d = get(url).json()
    rates = {x["AlphaCode"]: lrow(int(x.get("Units") or 1), num(x["Middle"]), num(x["Buy"]), num(x["Sell"]),
                                  compute_mid=False) for x in d["CurrencyExchangeItems"]}
    return {"valid_for": (d.get("Date") or "")[:10], "list_number": d.get("Number"), "url": url,
            "middle_source": "published", "rates": rates}


# --- North Macedonia: NBRM
def nbrm():
    t = dt.datetime.now(TZ_HR).date()
    url = (f"https://www.nbrm.mk/KLServiceNOV/GetExchangeRates?StartDate={(t - dt.timedelta(days=4)).strftime('%d.%m.%Y')}"
           f"&EndDate={t.strftime('%d.%m.%Y')}&format=json")
    d = get(url).json()
    latest = max(x["datum"] for x in d)
    rates = {x["oznaka"]: lrow(x.get("nomin") or 1, x.get("sreden"), x.get("kupoven"), x.get("prodazen"), compute_mid=False)
             for x in d if x["datum"] == latest}
    return {"valid_for": latest[:10], "url": url, "middle_source": "published", "rates": rates}


# --- Albania: Bank of Albania
def boa():
    url = "https://www.bankofalbania.org/Tregjet/Kursi_zyrtar_i_kembimit/"
    h = get(url).text
    rates = {}
    for c in _rows(h):
        if len(c) >= 3 and re.fullmatch(r"[A-Z]{3}", c[1] or "") and c[1] not in rates:
            m = re.search(r"\((\d+)\)", c[0])
            rates[c[1]] = lrow(int(m.group(1)) if m else 1, num(c[2]), compute_mid=False)
    if not rates:  # layout uses divs/tds without tr
        for m in re.finditer(r"<td[^>]*>\s*([^<]*?)\s*</td>\s*<td[^>]*>\s*([A-Z]{3})\s*</td>\s*<td[^>]*>\s*([\d.,]+)\s*</td>", h):
            u = re.search(r"\((\d+)\)", m.group(1))
            rates[m.group(2)] = lrow(int(u.group(1)) if u else 1, num(m.group(3)), compute_mid=False)
    d = re.search(r"(\d{2}\.\d{2}\.\d{4})", h[h.find("Përditesimi"):] if "Përditesimi" in h else "")
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"valid_for": dt.datetime.strptime(d.group(1), "%d.%m.%Y").date().isoformat() if d else None,
            "middle_source": "published", "rates": rates}


# --- Indonesia: Bank Indonesia (kurs transaksi + JISDOR), BCA, Mandiri
def bank_indonesia():
    url = "https://www.bi.go.id/id/statistik/informasi-kurs/transaksi-bi/default.aspx"
    h = get(url).text
    rates = {}
    for c in _rows(h):
        if len(c) >= 4 and re.fullmatch(r"[A-Z]{3}", c[0]) and num(c[2], "comma"):
            # Kurs Jual = BI sells foreign, Kurs Beli = BI buys foreign
            rates[c[0]] = lrow(int(num(c[1], "comma") or 1), None, num(c[3], "comma"), num(c[2], "comma"))
    out = {"middle_source": "computed_avg_transfer (USD: JISDOR)", "url": url, "rates": rates}
    try:
        jh = get("https://www.bi.go.id/id/statistik/informasi-kurs/jisdor/default.aspx").text
        for c in _rows(jh):
            if len(c) >= 2 and re.match(r"\d{1,2} \w+ \d{4}", c[0]):
                j = num(c[1].replace("Rp", ""), "comma")
                if j and "USD" in rates:
                    rates["USD"]["middle"] = r8(j)
                    rates["USD"]["jisdor"] = r8(j)
                    out["jisdor_date"] = c[0]
                break
    except Exception as e:
        out["jisdor_error"] = str(e)[:150]
    if not rates:
        raise RuntimeError("no rates parsed")
    return out


def bca():
    url = "https://www.bca.co.id/id/informasi/kurs"
    h = get(url).text
    i = h.find("m-table-kurs")
    t = h[i:h.find("</table>", i)]
    rates = {}
    for c in _rows(t):
        if len(c) >= 7 and re.fullmatch(r"[A-Z]{3}", c[0]):
            v = [num(x, "comma") for x in c[1:7]]
            # e-Rate (digital) buy/sell, TT Counter buy/sell, Bank Notes buy/sell
            rates[c[0]] = lrow(1, None, v[2], v[3], v[4], v[5])
            rates[c[0]]["e_rate"] = {"buy": r8(v[0]), "sell": r8(v[1])}
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"middle_source": "computed_avg_transfer", "note": "transfer = TT Counter, cash = Bank Notes, e_rate = digitalni kanali",
            "rates": rates}


def mandiri():
    url = "https://www.bankmandiri.co.id/kurs"
    h = get(url).text
    rates = {}
    for c in _rows(h):
        if len(c) >= 7 and re.fullmatch(r"[A-Z]{3}", c[0]):
            v = [num(x, "comma") for x in c[1:7]]
            rates[c[0]] = lrow(1, None, v[2], v[3], v[4], v[5])
            rates[c[0]]["special_rate"] = {"buy": r8(v[0]), "sell": r8(v[1])}
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"middle_source": "computed_avg_transfer", "note": "transfer = TT Counter, cash = Bank Notes",
            "rates": rates}


# --- Malaysia: BNM
def bnm():
    d = get("https://api.bnm.gov.my/public/exchange-rate", headers={"Accept": "application/vnd.BNM.API.v1+json"}).json()
    rates, day = {}, None
    for x in d["data"]:
        r = x["rate"]
        day = r.get("date") or day
        rates[x["currency_code"]] = lrow(x.get("unit") or 1, r.get("middle_rate"), r.get("buying_rate"),
                                         r.get("selling_rate"), compute_mid=False)
    return {"valid_for": day, "middle_source": "published", "note": "BNM interbank 12:00 rates", "rates": rates}


# --- Cambodia: NBC, ABA, ACLEDA
def nbc():
    url = "https://www.nbc.gov.kh/english/economic_research/exchange_rate.php"
    h = get(url).text
    rates = {}
    m = re.search(r"Official Exchange Rate\s*:\s*(?:<[^>]+>\s*)*([\d,.]+)", h)
    if m:
        rates["USD"] = lrow(1, num(m.group(1)), compute_mid=False)
    for c in _rows(h):
        if len(c) >= 6 and re.fullmatch(r"[A-Z]{3}/KHR", c[1]):
            rates[c[1][:3]] = lrow(int(num(c[2]) or 1), num(c[5]), num(c[3]), num(c[4]), compute_mid=False)
    d = re.search(r"Exchange Rate on\s*:\s*(?:<[^>]+>\s*)*(\d{4}-\d{2}-\d{2})", h)
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"valid_for": d.group(1) if d else None, "middle_source": "published", "rates": rates}


def aba():
    url = "https://www.ababank.com/en/forex-exchange/"
    h = get(url).text
    tabs = re.findall(r"<table.*?</table>", h, re.S)
    if len(tabs) < 2:
        raise RuntimeError("expected 2 tables (SWIFT transfer, notes)")
    tr = {c[0][:3]: c for c in _rows(tabs[0]) if len(c) >= 3 and re.match(r"[A-Z]{3} / KHR", c[0])}
    ca = {c[0][:3]: c for c in _rows(tabs[1]) if len(c) >= 3 and re.match(r"[A-Z]{3} / KHR", c[0])}
    rates = {}
    for cur in sorted(set(tr) | set(ca)):
        t, c = tr.get(cur, [None] * 3), ca.get(cur, [None] * 3)
        rates[cur] = lrow(1, None, num(t[1]), num(t[2]), num(c[1]), num(c[2]))
    return {"middle_source": "computed_avg_transfer", "rates": rates}


def acleda():
    url = "https://www.acledabank.com.kh/kh/eng/ps_cmforeignexchange"
    h = get(url).text
    rates = {}
    for m in re.finditer(r"exchange-flag/([A-Z]{3})\.png'\s*/>\s*[A-Z]{3}</td><td>([\d,.]+).*?</td><td>([\d,.]+)", h):
        if m.group(1) not in rates:
            rates[m.group(1)] = lrow(1, None, None, None, num(m.group(2)), num(m.group(3)))
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"middle_source": "computed_avg_cash", "note": "ACLEDA exchange counter (gotovina), KHR", "rates": rates}


# --- Laos: BCEL
def bcel():
    url = "https://www.bcel.com.la/bcel/exchange-rate.html"
    h = get(url).text
    rates = {}
    for c in _rows(h):
        if len(c) >= 7:
            m = re.match(r"([A-Z]{3})(?:\s+([\d-]+))?$", c[2])
            if not m:
                continue
            cur, den = m.group(1), m.group(2)
            if cur in rates and den and den.startswith("1-"):
                continue
            # buy: NOTE, BILL, EFT ; sell
            note, bill, eft, sell = [num(x) for x in c[3:7]]
            rates[cur] = lrow(1, None, eft, sell, note, sell)
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"middle_source": "computed_avg_transfer", "note": "cash = NOTE, transfer = EFT; USD/EUR: krupni apoeni", "rates": rates}


# --- Japan: MUFG (weekday TTM board)
def mufg():
    url = "https://www.bk.mufg.jp/ippan/kinri/list_j/kinri/kawase.html"
    r = get(url)
    r.encoding = r.apparent_encoding or "utf-8"
    rates = {}
    for c in _rows(r.text):
        if len(c) >= 9 and re.match(r"[A-Z]{3}", c[1]):
            cur = c[1][:3]
            tts, cash_s, ttb, cash_b = num(c[2]), num(c[4]), num(c[5]), num(c[8])
            if tts or ttb:
                rates[cur] = lrow(100 if cur in ("KRW", "IDR") else 1, None, ttb, tts, cash_b, cash_s)
    if not rates:
        raise RuntimeError("MUFG board empty (published only on Japanese business days)")
    return {"middle_source": "computed_avg_transfer (TTM)", "rates": rates}


# --- China: CFETS central parity, Bank of China
def cfets():
    url = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
    d = get(url).json()
    rates = {}
    for x in d.get("records", []):
        pair = x.get("vrtEName", "")
        p = num(x.get("price"))
        if not p:
            continue
        m = re.match(r"(100)?([A-Z]{3})/CNY$", pair)
        if m:
            rates[m.group(2)] = lrow(100 if m.group(1) else 1, p, compute_mid=False)
            continue
        m = re.match(r"CNY/([A-Z]{3})$", pair)
        if m:
            rates[m.group(1)] = lrow(1, 1 / p, compute_mid=False)
    return {"valid_for": (d.get("data") or {}).get("lastDate"), "middle_source": "published (central parity)", "rates": rates}


def boc_cn():
    url = "https://www.boc.cn/sourcedb/whpj/enindex_1619.html"
    r = get(url)
    r.encoding = "utf-8"
    rates, pub = {}, None
    for c in _rows(r.text):
        if len(c) >= 7 and re.fullmatch(r"[A-Z]{3}", c[0]):
            v = [num(x) for x in c[1:6]]
            rates[c[0]] = lrow(100, v[4], v[0], v[2], v[1], v[3])
            pub = pub or c[6]
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"published_at": pub, "middle_source": "published", "rates": rates}


# --- Sri Lanka: Bank of Ceylon
def boc_lk():
    url = "https://www.boc.lk/rates-tariff"
    h = get(url).text
    i = h.find("Telegraphic")
    t = h[h.rfind("<table", 0, i): h.find("</table>", i)]
    rates = {}
    for c in _rows(t):
        if len(c) >= 7 and re.fullmatch(r"[A-Z]{3}", c[0]):
            v = [num(x) for x in c[1:7]]
            rates[c[0]] = lrow(1, None, v[4], v[5], v[0], v[1])
    m = re.search(r"as at:\s*([\d.]+ [\d:]+ [AP]M)", t)
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"published_at": m.group(1) if m else None, "middle_source": "computed_avg_transfer",
            "note": "cash = Currency notes, transfer = Telegraphic transfers", "rates": rates}


# --- Turkey: TCMB, İşbank
def tcmb():
    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    root = ET.fromstring(get(url).content)
    rates = {}
    for c in root.findall("Currency"):
        cur = c.get("CurrencyCode")
        f = lambda k: num(c.findtext(k))
        u = int(c.findtext("Unit") or 1)
        if f("ForexBuying"):
            rates[cur] = lrow(u, None, f("ForexBuying"), f("ForexSelling"), f("BanknoteBuying"), f("BanknoteSelling"))
    d = root.get("Date")
    return {"valid_for": dt.datetime.strptime(d, "%m/%d/%Y").date().isoformat() if d else None,
            "list_number": root.get("Bulten_No"), "middle_source": "computed_avg_transfer (forex)", "rates": rates}


def isbank():
    url = "https://www.isbank.com.tr/en/foreign-exchange-rates"
    h = get(url).text
    t = re.findall(r"<table.*?</table>", h, re.S)
    rates = {}
    for c in _rows(t[0] if t else ""):
        if len(c) >= 3 and re.match(r"[A-Z]{3}\b", c[0]):
            rates[c[0][:3]] = lrow(1, None, num(c[1], "comma"), num(c[2], "comma"))
    if not rates:
        raise RuntimeError("no rates parsed")
    return {"middle_source": "computed_avg_transfer", "note": "İşbank FX board (live, changes during the day)", "rates": rates}


# --- Thailand: Bank of Thailand API (token in env BOT_TH_TOKEN)
def bot_th():
    tok = (os.environ.get("BOT_TH_TOKEN") or "").strip()
    if not tok:
        raise SkipSource("BOT_TH_TOKEN not set")
    end = dt.datetime.now(TZ_HR).date()
    q = f"?start_period={(end - dt.timedelta(days=7)).isoformat()}&end_period={end.isoformat()}"
    hdr = {"Authorization": tok, "Accept": "application/json"}
    base = "https://gateway.api.bot.or.th"
    det = get(f"{base}/Stat-ExchangeRate/v2/DAILY_AVG_EXG_RATE/{q}", headers=hdr).json()["result"]["data"]["data_detail"]
    det = [x for x in det if x.get("period")]
    if not det:
        raise RuntimeError("no data in the last 7 days")
    latest = max(x["period"] for x in det)
    rates = {}
    for x in det:
        if x["period"] != latest:
            continue
        m = re.search(r"\(([\d,]+)\s", x.get("currency_name_eng") or "")
        unit = int(m.group(1).replace(",", "")) if m else 1
        row = lrow(unit, num(x.get("mid_rate")), num(x.get("buying_transfer")), num(x.get("selling")), compute_mid=False)
        bs = num(x.get("buying_sight"))
        row["buying_sight"] = r8(bs / unit) if bs else None
        rates[x["currency_id"]] = row
    out = {"valid_for": latest, "middle_source": "published (BOT mid_rate)",
           "note": ("Prosjek komercijalnih banaka u Bangkoku (BOT). transfer.buy = buying_transfer (TT), "
                    "transfer.sell = selling, buying_sight = čekovi/mjenice; BOT ne objavljuje tečaj za gotovinu."),
           "url": f"{base}/Stat-ExchangeRate/v2/DAILY_AVG_EXG_RATE/{q}", "rates": rates}
    try:
        ref = get(f"{base}/Stat-ReferenceRate/v2/DAILY_REF_RATE/{q}", headers=hdr).json()["result"]["data"]["data_detail"]
        ref = [x for x in ref if x.get("period") and num(x.get("rate"))]
        if ref and "USD" in rates:
            r = max(ref, key=lambda x: x["period"])
            rates["USD"]["reference_rate"] = num(r["rate"])
            out["reference_rate_date"] = r["period"]
    except Exception as e:
        out["reference_rate_error"] = f"{type(e).__name__}"[:100]
    return out


TOURIST_SET = ["USD", "EUR", "GBP", "CHF", "JPY", "CNY", "AUD", "CAD", "NZD", "SGD", "HKD", "KRW", "THB", "IDR",
               "MYR", "PHP", "VND", "INR", "AED", "TRY", "PLN", "CZK", "HUF", "RON", "SEK", "NOK", "DKK", "RSD",
               "BAM", "MKD", "ALL", "KHR", "LAK", "LKR", "TWD", "SAR", "ILS", "BRL", "MXN", "ZAR", "EGP", "QAR"]


def global_as_central(local):
    """Fallback 'central' entry derived from open.er-api.com mid rates (for countries without a usable central bank feed)."""
    def fn():
        d = open_er_cached()
        r = d["rates_per_eur"]
        if local not in r:
            raise RuntimeError(f"{local} not in global reference")
        rates = {c: mk_row(1, r8(r[local] / r[c])) for c in TOURIST_SET if c in r and c != local}
        return {"valid_for": d["valid_for"], "middle_source": "global_reference (open.er-api.com mid)",
                "note": "Nema javno dohvatljivog tečaja središnje banke; prikazan je globalni referentni (mid) tečaj.",
                "rates": rates}
    return fn


_OER_CACHE = {}


def open_er_cached():
    with _LOCK:
        if "d" not in _OER_CACHE:
            _OER_CACHE["d"] = open_er()
        return _OER_CACHE["d"]


# ======================================================= registry
COUNTRIES = {
    "HR": {
        "name": "Hrvatska", "name_en": "Croatia", "local_currency": "EUR",
        "central_bank": source("hnb", "Hrvatska narodna banka (HNB)", "central_bank",
                               "https://api.hnb.hr/tecajn-eur/v3", "https://www.hnb.hr/temeljne-funkcije/monetarna-politika/tecajna-lista/tecajna-lista", hnb),
        "banks": [
            source("pbz", "Privredna banka Zagreb (PBZ)", "bank",
                   "https://www.pbz.hr/digitalServicesServlet/?operation=getExchangeRates&listType=PBZ&refCurrency=EUR",
                   "https://www.pbz.hr/gradjani/tecajna-lista-pbz.html", pbz),
            source("zaba", "Zagrebačka banka (UniCredit)", "bank", "https://www.zaba.hr/home/tecajnaDok/txt/<YYYY-MM-DD>",
                   "https://www.zaba.hr/home/tecajna", zaba),
            source("erste", "Erste&Steiermärkische Bank", "bank", "https://local.erstebank.hr/api/v1/fx",
                   "https://www.erstebank.hr/hr/tecajna-lista", erste,
                   enabled=os.environ.get("FX_ENABLE_ERSTE") == "1",
                   disabled_reason="robots.txt na local.erstebank.hr (host API-ja) ima 'Disallow: /'. Adapter radi; uključi s FX_ENABLE_ERSTE=1 tek nakon dopuštenja/odluke."),
            source("otp", "OTP banka", "bank", "https://www.otpbanka.hr/apps/elementa/getExchangeRates?date=<dd.mm.yyyy.>",
                   "https://www.otpbanka.hr/tecajne-liste", otp),
            source("rba", "Raiffeisenbank Austria (RBA)", "bank", "https://www.rba.hr/hr.exchangerates.<YYYYMMDD>.BASE.<valute>.EUR.TARGET.json",
                   "https://www.rba.hr/hr/tecajna-lista.html", rba),
            source("addiko", "Addiko Bank", "bank", "https://www.addiko.hr/gradanstvo/izracuni/tecajna-lista/",
                   "https://www.addiko.hr/gradanstvo/izracuni/tecajna-lista/", addiko),
        ],
    },
    "VN": {
        "name": "Vijetnam", "name_en": "Vietnam", "local_currency": "VND",
        "central_bank": source("sbv_central_via_tcb", "Ngân hàng Nhà nước (SBV) – tỷ giá trung tâm (preko Techcombank)",
                               "central_bank", "https://techcombank.com/.../ty-gia/_jcr_content.exchange-rates.integration.json (otherRate)",
                               "https://www.sbv.gov.vn/", sbv_central_via_tcb),
        "banks": [
            source("vietcombank", "Vietcombank", "bank", "https://www.vietcombank.com.vn/api/exchangerates?date=<YYYY-MM-DD>",
                   "https://www.vietcombank.com.vn/vi-VN/KHCN/Cong-cu-Tien-ich/Ty-gia", vietcombank),
            source("bidv", "BIDV", "bank", "https://bidv.com.vn/ServicesBIDV/ExchangeDetailServlet",
                   "https://bidv.com.vn/vn/ty-gia-ngoai-te", bidv),
            source("vietinbank", "VietinBank", "bank", "https://www.vietinbank.vn/ca-nhan/ty-gia-khcn",
                   "https://www.vietinbank.vn/ca-nhan/ty-gia-khcn", vietinbank),
            source("agribank", "Agribank", "bank", "https://www.agribank.com.vn/vn/ty-gia",
                   "https://www.agribank.com.vn/vn/ty-gia", agribank),
            source("techcombank", "Techcombank", "bank", "https://techcombank.com/content/techcombank/web/vn/vi/cong-cu-tien-ich/ty-gia/_jcr_content.exchange-rates.integration.json",
                   "https://techcombank.com/cong-cu-tien-ich/ty-gia", techcombank),
            source("acb", "ACB", "bank", "https://acb.com.vn/api/front/v1/currency",
                   "https://acb.com.vn/ty-gia-hoi-doai", acb),
        ],
    },
}


ECB_PAGE = "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html"
ECB_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def ecb_src(note=None):
    return source("ecb", "European Central Bank (ECB) – euro reference rates", "central_bank", ECB_XML, ECB_PAGE,
                  ecb_as_central(note))


def fallback_src(local, why):
    s = source("global_reference", "Globalni referentni tečaj (open.er-api.com)", "reference",
               "https://open.er-api.com/v6/latest/EUR", "https://www.exchangerate-api.com", global_as_central(local))
    s["why"] = why
    return s


EURO_NOTE_UNILATERAL = "Zemlja koristi euro (bez članstva u eurozoni) – referentni tečaj ECB-a."
WORLD = {
    # ---- eurozone (central = ECB)
    "DE": ("Njemačka", "Germany", "EUR", ecb_src(), []),
    "FR": ("Francuska", "France", "EUR", ecb_src(), []),
    "IT": ("Italija", "Italy", "EUR", ecb_src(), []),
    "ES": ("Španjolska", "Spain", "EUR", ecb_src(), []),
    "AT": ("Austrija", "Austria", "EUR", ecb_src(), []),
    "NL": ("Nizozemska", "Netherlands", "EUR", ecb_src(), []),
    "SI": ("Slovenija", "Slovenia", "EUR", ecb_src(), [
        source("nlb", "NLB", "bank", "https://www.nlb.si/services/tecajnica/?type=individuals&format=json",
               "https://www.nlb.si/tecajnica", nlb_si)]),
    "BG": ("Bugarska", "Bulgaria", "EUR", ecb_src("Bugarska koristi euro od 1.1.2026."), []),
    "ME": ("Crna Gora", "Montenegro", "EUR", ecb_src(EURO_NOTE_UNILATERAL), []),
    "XK": ("Kosovo", "Kosovo", "EUR", ecb_src(EURO_NOTE_UNILATERAL), []),
    # ---- EU non-euro
    "PL": ("Poljska", "Poland", "PLN", source("nbp", "Narodowy Bank Polski (NBP)", "central_bank",
            "https://api.nbp.pl/api/exchangerates/tables/{A,C}/?format=json", "https://nbp.pl/en/statistic-and-financial-reporting/rates/", nbp), []),
    "CZ": ("Češka", "Czechia", "CZK", source("cnb", "Česká národní banka (ČNB)", "central_bank",
            "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt",
            "https://www.cnb.cz/en/financial-markets/foreign-exchange-market/central-bank-exchange-rate-fixing/", cnb), [
        source("kb", "Komerční banka", "bank", "https://www.kb.cz/cs/kurzovni-listek (embedded JSON)",
               "https://www.kb.cz/cs/kurzovni-listek", kb_cz)]),
    "HU": ("Mađarska", "Hungary", "HUF", source("mnb", "Magyar Nemzeti Bank (MNB)", "central_bank",
            "http://www.mnb.hu/arfolyamok.asmx (SOAP GetCurrentExchangeRates)", "https://www.mnb.hu/en/arfolyamok", mnb), []),
    "RO": ("Rumunjska", "Romania", "RON", fallback_src("RON", "BNR XML (curs.bnr.ro) je zabranjen u robots.txt"), []),
    "SE": ("Švedska", "Sweden", "SEK", source("riksbank", "Sveriges Riksbank", "central_bank",
            "https://api.riksbank.se/swea/v1/Observations/Latest/ByGroup/130", "https://www.riksbank.se/en-gb/statistics/interest-rates-and-exchange-rates/", riksbank), []),
    "DK": ("Danska", "Denmark", "DKK", source("nationalbanken", "Danmarks Nationalbank", "central_bank",
            "https://www.nationalbanken.dk/api/currencyratesxml?lang=en", "https://www.nationalbanken.dk/en/what-we-do/stable-prices-monetary-policy-and-the-danish-economy/exchange-rates", nationalbanken), []),
    # ---- Balkan
    "RS": ("Srbija", "Serbia", "RSD", source("nbs", "Narodna banka Srbije (NBS)", "central_bank",
            "https://kurs.resenje.org/api/v1/rates/today", "https://www.nbs.rs/sr_RS/finansijsko_trziste/medjubankarsko-devizno-trziste/kursna-lista/", nbs_rs), []),
    "BA": ("Bosna i Hercegovina", "Bosnia and Herzegovina", "BAM", source("cbbh", "Centralna banka BiH (CBBH)", "central_bank",
            "https://www.cbbh.ba/CurrencyExchange/GetJson?date=<dd.mm.yyyy>", "https://www.cbbh.ba/CurrencyExchange/", cbbh), []),
    "MK": ("Sjeverna Makedonija", "North Macedonia", "MKD", source("nbrm", "Narodna banka RSM (NBRM)", "central_bank",
            "https://www.nbrm.mk/KLServiceNOV/GetExchangeRates?format=json", "https://www.nbrm.mk/kursna_lista.nspx", nbrm), []),
    "AL": ("Albanija", "Albania", "ALL", source("boa", "Banka e Shqipërisë", "central_bank",
            "https://www.bankofalbania.org/Tregjet/Kursi_zyrtar_i_kembimit/", "https://www.bankofalbania.org/Tregjet/Kursi_zyrtar_i_kembimit/", boa), []),
    # ---- Asia & Middle East
    "ID": ("Indonezija", "Indonesia", "IDR", source("bi", "Bank Indonesia (kurs transaksi + JISDOR)", "central_bank",
            "https://www.bi.go.id/id/statistik/informasi-kurs/transaksi-bi/default.aspx", "https://www.bi.go.id/id/statistik/informasi-kurs/jisdor/default.aspx", bank_indonesia), [
        source("bca", "Bank Central Asia (BCA)", "bank", "https://www.bca.co.id/id/informasi/kurs", "https://www.bca.co.id/id/informasi/kurs", bca),
        source("mandiri", "Bank Mandiri", "bank", "https://www.bankmandiri.co.id/kurs", "https://www.bankmandiri.co.id/kurs", mandiri)]),
    "TH": ("Tajland", "Thailand", "THB", source("bot", "Bank of Thailand (prosjek banaka u Bangkoku)", "central_bank",
            "https://gateway.api.bot.or.th/Stat-ExchangeRate/v2/DAILY_AVG_EXG_RATE/", "https://www.bot.or.th/en/statistics/exchange-rate.html", bot_th), []),
    "MY": ("Malezija", "Malaysia", "MYR", source("bnm", "Bank Negara Malaysia (BNM)", "central_bank",
            "https://api.bnm.gov.my/public/exchange-rate", "https://www.bnm.gov.my/exchange-rates", bnm), []),
    "SG": ("Singapur", "Singapore", "SGD", fallback_src("SGD", "MAS API nedostupan (maintenance stranica)"), []),
    "PH": ("Filipini", "Philippines", "PHP", fallback_src("PHP", "BSP stranica zabranjena u robots.txt"), []),
    "KH": ("Kambodža", "Cambodia", "KHR", source("nbc", "National Bank of Cambodia (NBC)", "central_bank",
            "https://www.nbc.gov.kh/english/economic_research/exchange_rate.php", "https://www.nbc.gov.kh/english/economic_research/exchange_rate.php", nbc), [
        source("aba", "ABA Bank", "bank", "https://www.ababank.com/en/forex-exchange/", "https://www.ababank.com/en/forex-exchange/", aba),
        source("acleda", "ACLEDA Bank", "bank", "https://www.acledabank.com.kh/kh/eng/ps_cmforeignexchange", "https://www.acledabank.com.kh/kh/eng/ps_cmforeignexchange", acleda)]),
    "LA": ("Laos", "Laos", "LAK", fallback_src("LAK", "bol.gov.la: TLS greška"), [
        source("bcel", "BCEL (Banque pour le Commerce Extérieur Lao)", "bank", "https://www.bcel.com.la/bcel/exchange-rate.html",
               "https://www.bcel.com.la/bcel/exchange-rate.html", bcel)]),
    "JP": ("Japan", "Japan", "JPY", fallback_src("JPY", "Bank of Japan ne objavljuje dnevnu referentnu listu"), [
        source("mufg", "MUFG Bank", "bank", "https://www.bk.mufg.jp/ippan/kinri/list_j/kinri/kawase.html",
               "https://www.bk.mufg.jp/ippan/kinri/list_j/kinri/kawase.html", mufg)]),
    "KR": ("Južna Koreja", "South Korea", "KRW", fallback_src("KRW", "BOK/Korea Exim API traže ključ; banke pune podatke JS-om"), []),
    "CN": ("Kina", "China", "CNY", source("cfets", "PBOC/CFETS central parity", "central_bank",
            "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json", "https://www.chinamoney.com.cn/english/bmkcpr/", cfets), [
        source("boc", "Bank of China", "bank", "https://www.boc.cn/sourcedb/whpj/enindex_1619.html", "https://www.boc.cn/sourcedb/whpj/enindex_1619.html", boc_cn)]),
    "IN": ("Indija", "India", "INR", fallback_src("INR", "RBI/FBIL nemaju jednostavan javni feed"), []),
    "LK": ("Šri Lanka", "Sri Lanka", "LKR", fallback_src("LKR", "CBSL stranica puni podatke JS-om"), [
        source("boc_lk", "Bank of Ceylon", "bank", "https://www.boc.lk/rates-tariff", "https://www.boc.lk/rates-tariff", boc_lk)]),
    "TR": ("Turska", "Turkey", "TRY", source("tcmb", "Türkiye Cumhuriyet Merkez Bankası (TCMB)", "central_bank",
            "https://www.tcmb.gov.tr/kurlar/today.xml", "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB+EN/Main+Menu/Statistics/Exchange+Rates/", tcmb), [
        source("isbank", "Türkiye İş Bankası", "bank", "https://www.isbank.com.tr/en/foreign-exchange-rates", "https://www.isbank.com.tr/en/foreign-exchange-rates", isbank)]),
    "AE": ("UAE", "United Arab Emirates", "AED", fallback_src("AED", "CBUAE vraća 403"), []),
}
for _cc, (_n, _ne, _cur, _cb, _banks) in WORLD.items():
    COUNTRIES[_cc] = {"name": _n, "name_en": _ne, "local_currency": _cur, "central_bank": _cb, "banks": _banks}


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


class SkipSource(Exception):
    """Raised by an adapter that cannot run in this environment (e.g. missing API token)."""


def run_source(s, cc=None, config=None):
    out = {k: s[k] for k in ("id", "name", "kind", "url", "page_url")}
    if s.get("why"):
        out["why"] = s["why"]
    out["fetched_at"] = now_utc()
    dis = ((config or {}).get("disabled") or {}).get(f"{cc}/{s['id']}")
    box = ((config or {}).get("box_only") or {}).get(f"{cc}/{s['id']}")
    if box:
        out.update(status="skipped", error=f"box-only source (merged from box_sources.json): {box}", rates={})
        return out
    if dis:
        out.update(status="skipped", error=f"disabled in sources_config.json: {dis}", rates={})
        return out
    if not s["enabled"]:
        out.update(status="skipped", error=s["disabled_reason"], rates={})
        return out
    t0 = time.time()
    try:
        res = s["fn"]()
        out.update(res)
        out["status"] = "ok"
        out["count"] = len(res.get("rates") or res.get("rates_per_eur") or {})
        if not out["count"]:
            raise RuntimeError("source returned 0 rates")
    except SkipSource as e:
        out.update(status="skipped", error=str(e), rates={})
        out.pop("count", None)
    except Exception as e:
        out.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}", rates={})
        out.pop("count", None)
        if os.environ.get("FX_DEBUG"):
            traceback.print_exc()
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    out.pop("fn", None)
    return out


def load_country_currency(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sanity(doc):
    """Compare bank middles with central bank / global reference. Returns warnings list."""
    warns = []
    ref = (doc["global_reference"].get("ecb") or {}).get("rates_per_eur") or \
          (doc["global_reference"].get("open_er_api") or {}).get("rates_per_eur") or {}
    er = (doc["global_reference"].get("open_er_api") or {}).get("rates_per_eur") or {}
    allref = {**er, **ref}

    def ref_local_per_foreign(local, foreign):
        if local in allref and foreign in allref:
            return allref[local] / allref[foreign]
        return None

    for cc, c in doc["countries"].items():
        local = c["local_currency"]
        for s in [c["central_bank"]] + c["banks"]:
            if s.get("status") not in ("ok", "stale"):
                continue
            for cur, row in s["rates"].items():
                r = ref_local_per_foreign(local, cur)
                # middle must be close to the reference (4 %); buy/sell spreads can be wide
                # for exotic cash (KRW, LAK cash in VN ~15-25 %), so allow 30 % there.
                checks = [("middle", row.get("middle"), 0.04)] + [
                    (f"{k}.{b}", row[k][b], 0.30) for k in ("transfer", "cash") for b in ("buy", "sell")]
                for label, v, lim in checks:
                    if v is None or r is None:
                        continue
                    dev = abs(v / r - 1)
                    if dev > lim:
                        warns.append(f"{cc}/{s['id']}/{cur} {label}: {v} vs ref {r:.6g} ({dev:.1%})")
                        break
                tb, ts, cb, cs = row["transfer"]["buy"], row["transfer"]["sell"], row["cash"]["buy"], row["cash"]["sell"]
                if tb and ts and tb > ts:
                    warns.append(f"{cc}/{s['id']}/{cur}: transfer buy {tb} > sell {ts}")
                if cb and cs and cb > cs:
                    warns.append(f"{cc}/{s['id']}/{cur}: cash buy {cb} > sell {cs}")
    # specific: EUR/VND must be ~ 24k-35k
    for s in (doc["countries"].get("VN") or {}).get("banks", []):
        e = (s.get("rates") or {}).get("EUR")
        if s.get("status") in ("ok", "stale") and e and e.get("middle") and not (24000 < e["middle"] < 35000):
            warns.append(f"VN/{s['id']}: EUR middle {e['middle']} out of expected 24k-35k")
    return warns


def carry_over(new, prev_by_id, now):
    """If a source failed, reuse its last good data from the previous rates.json, marked stale."""
    if new.get("status") != "error":
        return new
    p = prev_by_id.get(new["id"])
    if not p or p.get("status") not in ("ok", "stale") or not (p.get("rates") or p.get("rates_per_eur")):
        return new
    out = dict(p)                       # keeps original fetched_at, rates, valid_for, published_at ...
    out["status"] = "stale"
    out["stale_since"] = p.get("stale_since") or now
    out["last_error"] = new.get("error")
    out["last_attempt_at"] = new.get("fetched_at")
    out.pop("error", None)
    for k in ("name", "kind", "url", "page_url"):
        out[k] = new[k]
    return out


def index_prev(prev):
    idx = {}
    for cc, c in (prev.get("countries") or {}).items():
        for s in [c.get("central_bank") or {}] + (c.get("banks") or []):
            if s.get("id"):
                idx[(cc, s["id"])] = s
    for k, s in (prev.get("global_reference") or {}).items():
        idx[("global", k)] = s
    return idx


def _find_source(key):
    cc, sid = key.split("/", 1)
    c = COUNTRIES.get(cc) or {}
    for s in [c.get("central_bank")] + (c.get("banks") or []):
        if s and s["id"] == sid:
            return cc, s
    return cc, None


def box_main(a, config, sched, now_dt):
    """Box-side run: fetch the due 'box_only' sources (box schedule), write box_sources.json.
    Keeps entries of sources that were not due; keeps last good data (stale) on failure."""
    prev = {}
    try:
        with open(a.box_out, encoding="utf-8") as f:
            prev = json.load(f).get("sources", {})
    except Exception:
        pass
    keys = sorted(config.get("box_only") or {})
    due = [k for k in keys if a.force or k not in prev or
           is_due(_spec(sched, k, box=True), prev[k].get("checked_at") or prev[k].get("fetched_at"), now_dt)]
    if a.box_due:
        print(",".join(due))
        return 0 if due else 1
    if not due:
        print("no box source due")
        return 0
    out, now = dict((k, prev[k]) for k in keys if k in prev), now_utc()
    for key in due:
        cc, s = _find_source(key)
        if not s:
            print(f"  {key}: unknown source")
            continue
        res = run_source(s, cc, None)          # no config -> not skipped
        res = carry_over(res, {s["id"]: prev.get(key)} if prev.get(key) else {}, now)
        res["checked_at"] = now
        out[key] = res
        print(f"  {key}: {res['status']} {res.get('count', '')} {res.get('error') or res.get('last_error') or ''}")
    doc = {"generated_at": now, "fetched_on": "box", "max_age_hours": a.box_max_age_h,
           "next_check_at": {k: iso_utc(next_window(_spec(sched, k, box=True), now_dt)) for k in keys},
           "sources": out}
    with open(a.box_out + ".tmp", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(a.box_out + ".tmp", a.box_out)
    print(f"wrote {a.box_out} ({os.path.getsize(a.box_out)} bytes)")
    return 0 if any(out[k]["status"] == "ok" for k in due if k in out) else 1


def merge_box_sources(doc, path, config, max_age_h):
    keys = config.get("box_only") or {}
    if not keys:
        return
    try:
        with open(path, encoding="utf-8") as f:
            box = json.load(f).get("sources", {})
    except Exception:
        box = {}
    now = dt.datetime.now(dt.timezone.utc)
    for key in keys:
        cc, sid = key.split("/", 1)
        c = doc["countries"].get(cc)
        if not c:
            continue
        e = box.get(key)
        if not e or not e.get("rates"):
            continue                                   # stays 'skipped'
        e = dict(e, fetched_via="box")
        try:
            age_h = (now - dt.datetime.fromisoformat(e["fetched_at"])).total_seconds() / 3600
        except Exception:
            age_h = 1e9
        if e.get("status") == "ok" and age_h > max_age_h:
            e["status"] = "stale"
            e["stale_since"] = e.get("stale_since") or now.replace(microsecond=0).isoformat()
            e["last_error"] = f"box data is {age_h:.0f} h old (> {max_age_h:.0f} h) – box fetcher not running?"
        if c["central_bank"]["id"] == sid:
            c["central_bank"] = e
        else:
            c["banks"] = [e if b["id"] == sid else b for b in c["banks"]]


# ---------------------------------------------------------------- scheduling (per-country local windows)
def load_schedule(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_iso(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _spec(sched, key, box=False):
    d = sched.get("default") or {}
    if key == "global":
        c = sched.get("global") or {}
    elif box:
        c = (sched.get("box") or {}).get(key) or {}
    else:
        c = (sched.get("countries") or {}).get(key) or {}
    return (ZoneInfo(c.get("tz") or "UTC"),
            c.get("windows") or d.get("windows") or ["07:00"],
            c.get("weekend") or d.get("weekend") or c.get("windows") or d.get("windows") or ["07:00"])


def _window_times(spec, start_day, days):
    tz, wd, we = spec
    out = []
    for i in range(days):
        day = start_day + dt.timedelta(days=i)
        for w in (we if day.weekday() >= 5 else wd):
            hh, mm = map(int, w.split(":"))
            out.append(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz))
    return sorted(out)


def last_window(spec, now):
    """Most recent window start <= now (aware datetime)."""
    loc = now.astimezone(spec[0]).date()
    ws = [w for w in _window_times(spec, loc - dt.timedelta(days=3), 4) if w <= now]
    return ws[-1] if ws else None


def next_window(spec, now):
    loc = now.astimezone(spec[0]).date()
    ws = [w for w in _window_times(spec, loc, 4) if w > now]
    return ws[0] if ws else None


def is_due(spec, checked_at, now):
    lw = last_window(spec, now)
    c = _parse_iso(checked_at)
    return c is None or (lw is not None and c < lw)


def iso_utc(t):
    return t.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat() if t else None


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(here, "rates.json"))
    ap.add_argument("--countries-file", default=os.path.join(here, "country_currency.json"))
    ap.add_argument("--config", default=os.path.join(here, "sources_config.json"))
    ap.add_argument("--schedule", default=os.path.join(here, "schedule_config.json"))
    ap.add_argument("--split-dir", default=None, help="dir for per-country files (default: <out dir>/rates)")
    ap.add_argument("--only", default=None, help="comma list of country codes to (re)fetch now (testing)")
    ap.add_argument("--all", action="store_true", help="fetch every country + global now (workflow_dispatch)")
    ap.add_argument("--now", default=None, help="pretend current UTC time (ISO), for --plan simulation")
    ap.add_argument("--plan", action="store_true", help="only print which countries are due, fetch nothing")
    ap.add_argument("--no-published", action="store_true", help="drop original HR 'published' numbers (smaller JSON)")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--box-run", action="store_true",
                    help="fetch the sources in sources_config.json 'box_only' that are due (box schedule) and write --box-out")
    ap.add_argument("--box-due", action="store_true", help="print due box keys (comma list), exit 1 if none")
    ap.add_argument("--force", action="store_true", help="with --box-run: fetch all box sources regardless of schedule")
    ap.add_argument("--box-out", default=os.path.join(here, "box_sources.json"))
    ap.add_argument("--box-max-age-h", type=float, default=36.0)
    a = ap.parse_args()
    config = load_config(a.config)
    sched = load_schedule(a.schedule)
    now_dt = _parse_iso(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    if a.box_run or a.box_due:
        return box_main(a, config, sched, now_dt)

    prev = {}
    try:
        with open(a.out, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    pcountries = prev.get("countries") or {}

    # ---- which countries are due?
    if a.all:
        due = list(COUNTRIES)
        g_due = True
    elif a.only:
        due = [cc for cc in a.only.upper().split(",") if cc in COUNTRIES]
        g_due = True
    else:
        due = [cc for cc in COUNTRIES if cc not in pcountries or
               is_due(_spec(sched, cc), pcountries[cc].get("checked_at") or prev.get("checked_at"), now_dt)]
        g_due = not prev.get("global_reference") or is_due(_spec(sched, "global"),
                                                           prev.get("global_checked_at") or prev.get("checked_at"), now_dt)
    if a.plan:
        print(f"{iso_utc(now_dt)}  global={'due' if g_due else '-'}  due: {','.join(due) or '-'}")
        return 0

    started = now_utc()
    doc = {"schema_version": 1, "generated_at": started, "checked_at": started,
           "convention": ("countries.*: value = local currency per 1 unit of foreign currency; buy = bank buys "
                          "foreign from you, sell = bank sells foreign to you; transfer = devize/chuyển khoản, "
                          "cash = efektiva/tiền mặt. global_reference: units of currency per 1 EUR (mid)."),
           "refreshed_this_run": due, "countries": {}, "global_reference": {}}
    pidx = index_prev(prev)

    # ---- global references (only when due; otherwise keep previous block as-is)
    if g_due:
        gref = {}
        for key, fn, name, url in [
            ("ecb", ecb_cached, "European Central Bank – euro foreign exchange reference rates", ECB_XML),
            ("open_er_api", open_er_cached, "ExchangeRate-API open access (open.er-api.com)", "https://open.er-api.com/v6/latest/EUR"),
        ]:
            gref[key] = run_source(source(key, name, "reference", url, url, fn), "global", config)
        iso = set((gref["open_er_api"].get("rates_per_eur") or {}).keys()) | set((gref["ecb"].get("rates_per_eur") or {}).keys())
        gref["fawazahmed0"] = run_source(source("fawazahmed0", "fawazahmed0 currency-api (jsDelivr)", "reference",
                                                "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/eur.json",
                                                "https://github.com/fawazahmed0/exchange-api", lambda: fawaz(iso)), "global", config)
        for g in gref.values():
            g.pop("rates", None) if g.get("status") == "ok" else None
        gsub = {k[1]: v for k, v in pidx.items() if k[0] == "global"}
        for k in list(gref):
            gref[k] = carry_over(gref[k], gsub, started)
        doc["global_checked_at"] = started
    else:
        gref = prev.get("global_reference") or {}
        doc["global_checked_at"] = prev.get("global_checked_at") or prev.get("checked_at")
    iso = set()
    for k in ("open_er_api", "ecb"):
        iso |= set(((gref.get(k) or {}).get("rates_per_eur") or {}).keys())

    # ---- due country sources in parallel (different hosts; each adapter isolated)
    jobs = []
    for cc in due:
        c = COUNTRIES[cc]
        jobs.append((cc, "central", c["central_bank"]))
        jobs += [(cc, "bank", b) for b in c["banks"]]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=int(os.environ.get("FX_WORKERS", "8"))) as ex:
        results = list(ex.map(lambda j: run_source(j[2], j[0], config), jobs))
    fresh = {}
    for (cc, role, _), res in zip(jobs, results):
        c = COUNTRIES[cc]
        e = fresh.setdefault(cc, {"name": c["name"], "name_en": c["name_en"], "local_currency": c["local_currency"],
                                  "checked_at": started, "central_bank": None, "banks": []})
        if iso and res.get("rates"):          # drop legacy / non-ISO codes and the local currency itself
            res["rates"] = {k: v for k, v in res["rates"].items() if k in iso and k != c["local_currency"]}
            res["count"] = len(res["rates"])
        if a.no_published:
            for row in (res.get("rates") or {}).values():
                row.pop("published", None)
        if role == "central":
            e["central_bank"] = res
        else:
            e["banks"].append(res)
    for cc, e in fresh.items():                # fallback: carry over last good data for failed sources
        sub = {k[1]: v for k, v in pidx.items() if k[0] == cc}
        e["central_bank"] = carry_over(e["central_bank"], sub, started)
        e["banks"] = [carry_over(b, sub, started) for b in e["banks"]]
    for cc in COUNTRIES:                       # keep config order; not-due countries unchanged (not stale)
        if cc in fresh:
            doc["countries"][cc] = fresh[cc]
        elif cc in pcountries:
            doc["countries"][cc] = dict(pcountries[cc])
            doc["countries"][cc].setdefault("checked_at", prev.get("checked_at"))
    doc["global_reference"] = gref
    cc_map = load_country_currency(a.countries_file)
    if cc_map:
        doc["country_currency"] = cc_map

    # ---- merge box-fetched sources every run (sources that block GitHub runners; 36 h stale rule)
    merge_box_sources(doc, a.box_out, config, a.box_max_age_h)

    doc["sanity_warnings"] = sanity(doc)
    doc["summary"] = {
        cc: {s["id"]: s["status"] for s in [c["central_bank"]] + c["banks"]} for cc, c in doc["countries"].items()
    }
    doc["summary"]["global"] = {k: v["status"] for k, v in gref.items()}
    nxt = {cc: iso_utc(next_window(_spec(sched, cc), now_dt)) for cc in doc["countries"]}
    doc["next_refresh_at"] = dict(nxt, _global=iso_utc(next_window(_spec(sched, "global"), now_dt)))
    doc["finished_at"] = now_utc()

    dump = lambda obj, path: (json.dump(obj, open(path + ".tmp", "w", encoding="utf-8"), ensure_ascii=False,
                                        indent=2 if a.pretty else None, separators=None if a.pretty else (",", ":")),
                              os.replace(path + ".tmp", path))
    dump(doc, a.out)

    # per-country files + index (rates/<ISO2>.json, rates/index.json). A country file's own timestamps are that
    # country's checked_at, so files of countries that were not refreshed stay byte-identical.
    split = a.split_dir or os.path.join(os.path.dirname(os.path.abspath(a.out)), "rates")
    os.makedirs(split, exist_ok=True)
    index = {"schema_version": 1, "generated_at": doc["generated_at"], "checked_at": doc["checked_at"],
             "global_checked_at": doc["global_checked_at"], "convention": doc["convention"],
             "global_reference_file": "global.json", "countries": {}}
    for cc, c in doc["countries"].items():
        dump({"generated_at": c["checked_at"], "convention": doc["convention"], "country": cc, **c},
             os.path.join(split, f"{cc}.json"))
        index["countries"][cc] = {"name": c["name"], "name_en": c["name_en"], "local_currency": c["local_currency"],
                                  "file": f"{cc}.json", "checked_at": c["checked_at"], "next_refresh_at": nxt[cc],
                                  "sources": doc["summary"][cc]}
    dump({"generated_at": doc["global_checked_at"], "global_reference": gref, "country_currency": cc_map},
         os.path.join(split, "global.json"))
    dump(index, os.path.join(split, "index.json"))

    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write(f"due={','.join(due)}\nglobal_due={'1' if g_due else ''}\n")
    print(f"refreshed: {','.join(due) or '-'}  global: {'yes' if g_due else 'no'}")
    print(json.dumps({cc: doc["summary"][cc] for cc in due + (["global"] if g_due else [])}, ensure_ascii=False))
    for cc, c in doc["countries"].items():
        for s in [c["central_bank"]] + c["banks"]:
            if s["status"] != "ok" and (cc in due or s.get("fetched_via") == "box"):
                print(f"  {cc}/{s['id']}: {s['status']} – {s.get('error') or s.get('last_error')}"
                      + (f" (data from {s.get('fetched_at')})" if s["status"] == "stale" else ""))
    if doc["sanity_warnings"]:
        print("SANITY WARNINGS:", *doc["sanity_warnings"], sep="\n  ")
    print(f"wrote {a.out} ({os.path.getsize(a.out)} bytes) + {split}/ ({len(doc['countries'])} countries)")
    ok_any = any(s["status"] in ("ok", "stale") for c in doc["countries"].values() for s in [c["central_bank"]] + c["banks"])
    return 0 if ok_any or not due else 1


if __name__ == "__main__":
    sys.exit(main())
