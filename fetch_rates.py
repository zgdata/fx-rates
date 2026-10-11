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


def tcb_data():
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


def run_source(s):
    out = {k: s[k] for k in ("id", "name", "kind", "url", "page_url")}
    out["fetched_at"] = now_utc()
    if not s["enabled"]:
        out.update(status="skipped", error=s["disabled_reason"], rates={})
        return out
    t0 = time.time()
    try:
        res = s["fn"]()
        out.update(res)
        out["status"] = "ok"
        out["count"] = len(res.get("rates") or res.get("rates_per_eur") or {})
    except Exception as e:
        out.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}", rates={})
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
    for s in doc["countries"]["VN"]["banks"]:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "rates.json"))
    ap.add_argument("--countries-file", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "country_currency.json"))
    ap.add_argument("--no-published", action="store_true", help="drop original HR 'published' numbers (smaller JSON)")
    ap.add_argument("--pretty", action="store_true")
    a = ap.parse_args()

    started = now_utc()
    doc = {"schema_version": 1, "generated_at": started,
           "convention": ("countries.*: value = local currency per 1 unit of foreign currency; buy = bank buys "
                          "foreign from you, sell = bank sells foreign to you; transfer = devize/chuyển khoản, "
                          "cash = efektiva/tiền mặt. global_reference: units of currency per 1 EUR (mid)."),
           "countries": {}, "global_reference": {}}

    for cc, c in COUNTRIES.items():
        cb = run_source(c["central_bank"])
        banks = [run_source(b) for b in c["banks"]]
        if a.no_published:
            for s in [cb] + banks:
                for row in (s.get("rates") or {}).values():
                    row.pop("published", None)
        doc["countries"][cc] = {"name": c["name"], "name_en": c["name_en"], "local_currency": c["local_currency"],
                                "central_bank": cb, "banks": banks}

    gref = {}
    for key, fn, name, url in [
        ("ecb", ecb, "European Central Bank – euro foreign exchange reference rates", "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"),
        ("open_er_api", open_er, "ExchangeRate-API open access (open.er-api.com)", "https://open.er-api.com/v6/latest/EUR"),
    ]:
        gref[key] = run_source(source(key, name, "reference", url, url, fn))
    iso = set((gref["open_er_api"].get("rates_per_eur") or {}).keys()) | set((gref["ecb"].get("rates_per_eur") or {}).keys())
    gref["fawazahmed0"] = run_source(source("fawazahmed0", "fawazahmed0 currency-api (jsDelivr)", "reference",
                                            "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/eur.json",
                                            "https://github.com/fawazahmed0/exchange-api", lambda: fawaz(iso)))
    for g in gref.values():
        g.pop("rates", None) if g.get("status") == "ok" else None
    doc["global_reference"] = gref
    cc_map = load_country_currency(a.countries_file)
    if cc_map:
        doc["country_currency"] = cc_map

    # ---- fallback: carry over last good data for failed sources
    prev = {}
    try:
        with open(a.out, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    pidx = index_prev(prev)
    now = now_utc()
    for cc, c in doc["countries"].items():
        sub = {k[1]: v for k, v in pidx.items() if k[0] == cc}
        c["central_bank"] = carry_over(c["central_bank"], sub, now)
        c["banks"] = [carry_over(b, sub, now) for b in c["banks"]]
    gsub = {k[1]: v for k, v in pidx.items() if k[0] == "global"}
    for k in list(gref):
        gref[k] = carry_over(gref[k], gsub, now)

    doc["sanity_warnings"] = sanity(doc)
    doc["summary"] = {
        cc: {s["id"]: s["status"] for s in [c["central_bank"]] + c["banks"]} for cc, c in doc["countries"].items()
    }
    doc["summary"]["global"] = {k: v["status"] for k, v in gref.items()}
    doc["finished_at"] = now_utc()

    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2 if a.pretty else None, separators=None if a.pretty else (",", ":"))
    os.replace(tmp, a.out)

    print(json.dumps(doc["summary"], ensure_ascii=False, indent=1))
    for cc, c in doc["countries"].items():
        for s in [c["central_bank"]] + c["banks"]:
            if s["status"] != "ok":
                print(f"  {cc}/{s['id']}: {s['status']} – {s.get('error') or s.get('last_error')}"
                      + (f" (data from {s.get('fetched_at')})" if s["status"] == "stale" else ""))
    if doc["sanity_warnings"]:
        print("SANITY WARNINGS:", *doc["sanity_warnings"], sep="\n  ")
    print(f"wrote {a.out} ({os.path.getsize(a.out)} bytes)")
    ok_any = any(s["status"] in ("ok", "stale") for c in doc["countries"].values() for s in c["banks"])
    return 0 if ok_any else 1


if __name__ == "__main__":
    sys.exit(main())
