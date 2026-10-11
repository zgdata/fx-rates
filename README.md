# fx-rates – dnevni dohvat tečajeva za travel converter (PWA)

**Podaci (osvježava se po državi, u lokalno jutro / prijepodne / popodne te države – vidi `schedule_config.json`):**
- Sve u jednoj datoteci: `https://zgdata.github.io/fx-rates/rates.json`
- Po državi: `https://zgdata.github.io/fx-rates/rates/<ISO2>.json` (npr. `rates/ID.json`), popis: `rates/index.json`,
  globalni referentni tečajevi + mapa država→valuta: `rates/global.json`
- jsDelivr CDN (backup): `https://cdn.jsdelivr.net/gh/zgdata/fx-rates@main/rates.json` (CDN cache do ~12 h, browser do 7 dana – koristi Pages kao primarni)
- `countries.<CC>.checked_at` = kad je ta država zadnji put provjerena (za "AI bot provjerio"); vrh datoteke `generated_at`/`checked_at` = zadnji commit, `global_checked_at` = globalni izvori.
- `rates/index.json` → `countries.<CC>.next_refresh_at` (UTC) = sljedeći planirani prozor (stvarni dohvat je prvi satni run nakon toga, tj. do ~1 h kasnije).

> **Atribucija / napomena (obavezno prikazati u appu):**
> - Globalni referentni tečajevi iz open.er-api.com: **"Rates By Exchange Rate API"** (https://www.exchangerate-api.com).
> - Tečajevi banaka su **informativni**; preuzeti s javnih stranica banaka, mogu kasniti ili se razlikovati od tečaja u poslovnici. Za stvarnu transakciju vrijedi tečaj banke.
> - ECB referentni tečajevi služe samo za informaciju.

Jedna skripta (`fetch_rates.py`) → jedna datoteka (`rates.json`). App je povuče 1× dnevno,
spremi offline (Cache API / IndexedDB) i radi bez interneta.

## Pokretanje
```bash
pip install requests          # jedina ovisnost (Python 3.9+)
python3 fetch_rates.py        # piše ./rates.json (kompaktno, ~58 KB, ~15 KB gzip)
python3 fetch_rates.py --pretty --no-published   # čitljivo / bez originalnih HR brojeva
FX_DEBUG=1 python3 fetch_rates.py               # ispiše traceback za banke koje padnu
```
Izlazni kod 0 ako je barem jedna banka uspjela (ok ili stale). Svaka banka je izolirana.

**Fallback (stale):** ako izvor padne, a u prethodnom `rates.json` postoje njegovi podaci, oni se
prenose i označe `status:"stale"`, uz originalni `fetched_at` (kad su podaci zaista dohvaćeni),
`stale_since` (prvi neuspjeli pokušaj), `last_error` i `last_attempt_at`. Ako prethodnih podataka nema,
ostaje `status:"error"` + `error`. App: za `stale` prikaži "zadnje provjereno <fetched_at>".

Opt-in prekidači (po defaultu ISKLJUČENI, vidi "Pravila"):
- `FX_ENABLE_ERSTE=1` – Erste (API host `local.erstebank.hr` ima robots.txt `Disallow: /`)
- `FX_OTP_BROWSER_UA=1` – OTP (WAF vraća 403 za svaki ne-browser User-Agent)

## Države (36)
HR, VN (banke), eurozona DE FR IT ES AT NL SI BG ME XK (ECB; SI + NLB), PL (NBP), CZ (ČNB + KB),
HU (MNB), RO (globalni fallback), SE (Riksbank), DK (Nationalbanken), RS (NBS), BA (CBBH), MK (NBRM),
AL (BoA), ID (Bank Indonesia + BCA + Mandiri), MY (BNM), KH (NBC + ABA + ACLEDA), LA (BCEL), JP (MUFG),
CN (CFETS + Bank of China), LK (Bank of Ceylon), TR (TCMB + İşbank), TH (Bank of Thailand API, token u secretu `BOT_TH_TOKEN`), te SG, PH, KR, IN, AE (globalni fallback).
Gdje nema središnje banke, `central_bank` je `{"id":"global_reference","kind":"reference"}` izveden iz open.er-api.com
(mid, ~40 turističkih valuta), s poljem `why`.

## Isključivanje izvora (`sources_config.json`)
`{"disabled": {"VN/vietinbank": "razlog"}}` – izvor dobije `status:"skipped"` (bez stale podataka).
Koristi se za izvore koji ne rade s GitHub Actions runnera.

## Izvori (HR + VN; ostale države: `url` / `page_url` u JSON-u)
| Država | Izvor | Endpoint | Format |
|---|---|---|---|
| HR | HNB (središnja) | `api.hnb.hr/tecajn-eur/v3` | službeni JSON API |
| HR | PBZ | `pbz.hr/digitalServicesServlet/?operation=getExchangeRates&listType=PBZ…` | JSON (koristi ga njihova stranica) |
| HR | Zagrebačka banka | `zaba.hr/home/tecajnaDok/txt/<YYYY-MM-DD>` | službeni TXT download |
| HR | RBA | `rba.hr/hr.exchangerates.<YYYYMMDD>.BASE.<valute>.EUR.TARGET.json` | JSON (njihova stranica) |
| HR | Addiko | `addiko.hr/gradanstvo/izracuni/tecajna-lista/` | HTML tablica |
| HR | OTP | `otpbanka.hr/apps/elementa/getExchangeRates?date=dd.mm.yyyy.` | JSON – opt-in |
| HR | Erste | `local.erstebank.hr/api/v1/fx` | JSON – opt-in |
| VN | SBV tỷ giá trung tâm (USD) | iz Techcombank JSON-a (`otherRate`) | sbv.gov.vn odbija veze izvan VN |
| VN | Vietcombank | `vietcombank.com.vn/api/exchangerates?date=` (fallback XML `pXML.aspx`) | JSON / XML |
| VN | BIDV | `bidv.com.vn/ServicesBIDV/ExchangeDetailServlet` | JSON |
| VN | VietinBank | `vietinbank.vn/ca-nhan/ty-gia-khcn` | HTML (SSR) |
| VN | Agribank | `agribank.com.vn/vn/ty-gia` | HTML tablica |
| VN | Techcombank | `techcombank.com/…/ty-gia/_jcr_content.exchange-rates.integration.json` | JSON |
| VN | ACB | `acb.com.vn/api/front/v1/currency?currency=VND&effectiveDateTime=` | JSON |
| svijet | ECB | `ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` | ~30 valuta, radnim danom |
| svijet | ExchangeRate-API | `open.er-api.com/v6/latest/EUR` | ~166 valuta, bez ključa, **traži atribuciju** |
| svijet | fawazahmed0 | `cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/eur.json` | ~160 fiat (krypto izbačen) |

`country_currency.json` (statično, iz datasets/country-codes, ISO 3166 → ISO 4217) mapira
svaku državu na valutu, pa svaka država ima barem referentni (mid) tečaj iz `global_reference`.

## Konvencija brojeva (VAŽNO)
U `countries.<CC>.*.rates.<VALUTA>`:
**vrijednost = koliko LOKALNE valute vrijedi 1 jedinica strane valute.**
- HR (EUR): `USD.middle = 0.8924` → 1 USD = 0,8924 EUR
- VN (VND): `EUR.middle = 29222` → 1 EUR = 29.222 VND
- `buy` = banka KUPUJE stranu valutu od tebe (ti prodaješ), `sell` = banka PRODAJE tebi. Uvijek buy ≤ middle ≤ sell.
- `transfer` = devize / bezgotovinski / chuyển khoản; `cash` = efektiva / gotovina / tiền mặt.
- HR banke objavljuju obrnuto ("1 EUR = 1,1206 USD"). Skripta invertira; originalni brojevi su u `published`
  (makni ih s `--no-published`). Za prikaz "1 EUR = x USD" u appu: `1 / middle`, a za kupovni/prodajni
  zamijeni: `1/transfer.buy` = HR "kupovni", `1/transfer.sell` = HR "prodajni".
- VN banke ne objavljuju srednji tečaj → `middle` = prosjek (transfer.buy + transfer.sell)/2,
  označeno s `middle_source:"computed_avg_transfer"`. HR banke: `"published"`.
- `null` = banka tu vrstu tečaja ne nudi (npr. HNB nema efektivu, mnoge banke nemaju cash za DKK/NOK/SEK).
- USD/EUR u VN: uzima se tečaj za krupne apoene (50/100); manji apoeni se ignoriraju.

U `global_reference.<izvor>.rates_per_eur`: **koliko jedinica valute za 1 EUR** (mid).
Kros tečaj X→Y = `rates_per_eur[Y] / rates_per_eur[X]`.

## Shema (rates.json)
```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-10-11T14:52:10+00:00",      // UTC; = "AI bot provjerio" timestamp
  "finished_at": "…",
  "convention": "…tekstualni opis gore…",
  "countries": {
    "HR": {
      "name": "Hrvatska", "name_en": "Croatia", "local_currency": "EUR",
      "central_bank": <Source>,
      "banks": [<Source>, …]
    },
    "VN": { … }
  },
  "global_reference": {
    "ecb":          { …Source polja…, "valid_for": "2026-10-09", "rates_per_eur": {"USD":1.1206,…} },
    "open_er_api":  { …, "attribution": "Rates By Exchange Rate API (https://www.exchangerate-api.com)", "rates_per_eur": {…} },
    "fawazahmed0":  { …, "rates_per_eur": {…} }
  },
  "country_currency": { "HR": {"currency":"EUR","name_en":"Croatia"}, "VN": {"currency":"VND",…}, … },
  "sanity_warnings": ["VN/bidv/LAK middle: 1.05 vs ref 1.167 (10.1%)"],
  "summary": { "HR": {"hnb":"ok","pbz":"ok",…,"otp":"error"}, "VN": {…}, "global": {…} }
}

<Source> = {
  "id": "pbz", "name": "Privredna banka Zagreb (PBZ)", "kind": "bank|central_bank|reference",
  "url": "endpoint koji je skripta zvala", "page_url": "javna stranica za link 'izvor' u appu",
  "status": "ok|stale|error|skipped", "error": "poruka (error/skipped)",
  "stale_since": "UTC ISO (samo stale)", "last_error": "…", "last_attempt_at": "UTC ISO",
  "fetched_at": "UTC ISO", "elapsed_ms": 420, "count": 13,
  "valid_for": "YYYY-MM-DD (datum primjene liste, ako ga banka daje)",
  "published_at": "ISO s offsetom (VN banke – vrijeme objave)",
  "list_number": 199,
  "middle_source": "published|computed_avg_transfer",
  "rates": { "USD": {
      "middle": 0.89317613,
      "transfer": {"buy": 0.8671613, "sell": 0.92080014},
      "cash":     {"buy": 0.85857545, "sell": 0.93010105},
      "published": {…samo HR, originalni "1 EUR = x" brojevi…},
      "floor": …, "ceiling": …            // samo SBV središnji tečaj
  } }
}
```

## Provjera ispravnosti
`sanity_warnings` nastaju automatski: middle banke vs ECB/open.er-api > 4 %, buy/sell > 30 %,
buy > sell, i EUR/VND izvan 24k–35k. Upozorenja za LAK su očekivana (službeni i tržišni kip se razlikuju).

## Pravila / ToS
- Skripta se predstavlja vlastitim User-Agentom (`TravelFXFetcher/1.0`), radi 1 request po izvoru po prozoru (1–5× dnevno, vidi `schedule_config.json`).
- Bez zaobilaženja captcha/WAF-a. Erste i OTP su zato isključeni dok ne odlučiš (vidi gore).
- open.er-api.com: besplatan bez ključa, ali traži vidljivu atribuciju u appu i max ~1 poziv/dan je sasvim OK.
- Vietcombank XML feed: max 1 request / 5 min (mi radimo ≤ 5/dan).
- Tečajevi banaka su informativni; u appu napiši "informativno, provjeri u banci".

## Automatizacija (GitHub Actions)
`.github/workflows/daily.yml`: cron `23 * * * *` (svaki sat) + ručni `workflow_dispatch` (= dohvati sve).
`schedule_config.json` za svaku državu ima IANA zonu i lokalne prozore (default `07:00, 09:00, 12:00, 18:00`,
vikendom `07:00`; prilagođeno vremenu objave, npr. ECB 16:30, CNB 14:45, BOT 18:30, JISDOR 10:30).
Država je "due" kad je njen `checked_at` stariji od zadnjeg prozora koji je već počeo – pa propušteni / zakašnjeli
runovi automatski nadoknade. Svaki run: učita prethodni `rates.json`, ponovno dohvati samo due države
(ostale ostaju kakve jesu, nisu stale), osvježi njihov `checked_at`, i uvijek spoji `box_sources.json`.
Globalni izvori (ECB, open.er-api, fawazahmed0) imaju svoje prozore (UTC 00:30, 06, 12, 15, 18; vikendom 00:30, 12).
Commit samo ako je neka država bila due (heartbeat) ili su se podaci promijenili (`scripts/changed.py` ignorira
vremenske oznake); inače se promjene odbace → nema praznih satnih commitova. jsDelivr purge samo za promijenjene datoteke.
Simulacija: `python fetch_rates.py --plan --now 2026-10-12T05:23Z`.
GitHub Pages servira iz `main` / root (`.nojekyll` isključuje Jekyll). Pages šalje
`Access-Control-Allow-Origin: *`, pa PWA može `fetch()` direktno.

## Izvori koji blokiraju GitHub (box fetcher)
VietinBank (VN), ABA (KH) i Bank of Ceylon (LK) vraćaju 403/405 GitHub runnerima. Oni su u
`sources_config.json` pod `box_only`: GitHub ih preskače, a dohvaća ih `run_box.sh` na zasebnom računalu
("box") u `box_sources.json` (commit `box: …`). Workflow ih spaja u `rates.json` / `rates/<CC>.json`
s poljem `fetched_via:"box"`; ako su stariji od 36 h, dobiju `status:"stale"` + `last_error`.
Cron na boxu: `*/30 * * * * …/run_box.sh` (+ `@reboot`); `schedule_config.json` → `box` određuje što je due:
VietinBank i ABA 07:30 i 12:00 (ICT), Bank of Ceylon 08:30 i 12:00 (IST); propušteni prozor se nadoknadi pri sljedećem pozivu.
**Cloudflare Worker (primarno od 11.10.2026.):** `cloudflare/` (worker.js, wrangler.toml) dohvaća iste tri banke
(cron VN/KH 07:30 + 12:30, LK 08:30 + 12:30 lokalno) i servira https://fx-box-sources.stjepan-20b.workers.dev/box_sources.json
(`fetched_via:"cloudflare"`). Workflow ga prije `fetch_rates.py` povuče (greška se tolerira) i `scripts/merge_cf.py`
ga spoji s commitanim `box_sources.json` (po izvoru pobjeđuje najnoviji `ok` unos). Box cron ostaje kao backup.
Izvori postaju stale tek ako ni Worker ni box nisu imali uspješan dohvat 36 h.
