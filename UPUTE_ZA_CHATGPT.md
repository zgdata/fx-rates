# Podatkovni backend za moju app za pretvorbu valuta (fx-rates)

Ovo je opis gotovog sustava koji dohvaća tečajeve. Koristi ga kao izvor istine kad radiš na PWA aplikaciji. App NE zove banke izravno, nego čita samo ove JSON datoteke.

## 1. Gdje je što

| Što | Gdje |
|---|---|
| GitHub repozitorij (kod + podaci) | https://github.com/zgdata/fx-rates (javan, račun `zgdata`) |
| Dokumentacija sheme | README.md u repozitoriju |
| Glavni URL podataka (GitHub Pages, CORS `*`, cache ~10 min) | https://zgdata.github.io/fx-rates/ |
| Popis država + status + vremena | https://zgdata.github.io/fx-rates/rates/index.json |
| Jedna država | https://zgdata.github.io/fx-rates/rates/<ISO2>.json (npr. `HR.json`, `VN.json`, `TH.json`, `ID.json`) |
| Svjetski referentni tečajevi + karta država→valuta (249 država) | https://zgdata.github.io/fx-rates/rates/global.json |
| Sve u jednom (~200 KB, ~32 KB gzip) | https://zgdata.github.io/fx-rates/rates.json |
| Rezervni CDN (može kasniti do 12 h, samo kao fallback) | https://cdn.jsdelivr.net/gh/zgdata/fx-rates@main/rates/index.json (isti put za ostale datoteke) |
| Cloudflare Worker za 3 banke koje blokiraju GitHub | https://fx-box-sources.stjepan-20b.workers.dev/box_sources.json (app ga NE treba čitati, workflow ga sam spaja) |

Datoteke u repozitoriju:
- `fetch_rates.py`: glavna Python skripta, jedan adapter po banci/izvoru, svaki pada neovisno.
- `sources_config.json`: koji su izvori uključeni, isključeni ili `box_only`.
- `schedule_config.json`: vremenska zona i lokalna vremena osvježavanja po državi.
- `country_currency.json`: država → valuta (statično).
- `.github/workflows/daily.yml`: GitHub Actions, pokreće se svaki sat u :23 UTC i ručno (workflow_dispatch = dohvati sve).
- `scripts/merge_cf.py`: spaja podatke s Cloudflare Workera.
- `cloudflare/`: kod Workera (`worker.js`, `wrangler.toml`).
- `box_sources.json` + `run_box.sh`: rezervni dohvat s mog servera za iste 3 banke.

## 2. Pokrivenost (36 država)

S bankama:
- **HR:** HNB (središnja), PBZ, Zagrebačka banka, RBA, Addiko. Erste je isključen (robots.txt zabranjuje), OTP je isključen (blokira automatske upite).
- **VN:** SBV središnji tečaj (samo USD, preko Techcombanka), Vietcombank, BIDV, VietinBank, Agribank, Techcombank, ACB.
- **ID:** Bank Indonesia (+ JISDOR USD), BCA, Mandiri. BRI i BNI imaju zaštitu od botova, pa su isključeni.
- **TH:** Bank of Thailand, službeni API s ključem (prosjek banaka u Bangkoku za 19 valuta + THB/USD referentni). Gotovinski tečaj BOT ne objavljuje.
- **SI:** ECB + NLB.
- **CZ:** ČNB + Komerční banka.
- **KH:** NBC, ACLEDA, ABA.
- **LA:** BCEL + globalna referenca.
- **CN:** CFETS + Bank of China.
- **TR:** TCMB + İşbank.
- **LK:** Bank of Ceylon + globalna referenca.
- **JP:** MUFG (radi samo radnim danima) + globalna referenca.

Samo središnja banka:
- **PL** NBP, **HU** MNB, **SE** Riksbank, **DK** Nationalbanken, **RS** NBS (preko kurs.resenje.org), **BA** CBBH, **MK** NBRM, **AL** Bank of Albania, **MY** BNM.

Samo ECB (euro zemlje):
- DE, FR, IT, ES, AT, NL, BG (euro od 2026.), ME, XK.

Samo globalna referenca:
- RO, SG, PH, KR, IN, AE.

Sve ostale države svijeta koriste `global.json`.

## 3. Konvencija brojeva (najvažnije)

- Unutar države svaki tečaj znači: **koliko LOKALNE valute vrijedi 1 jedinica strane valute**.
  - VN: `rates.EUR.middle = 29222` znači 1 EUR = 29.222 VND.
  - HR: `rates.USD.middle = 0.8924` znači 1 USD = 0,8924 EUR.
- Hrvatske banke objavljuju obrnuto (1 EUR = 1,12 USD). Skripta to okreće, a originalne brojke čuva u `published`. Za prikaz "hrvatskog" oblika koristi `1/middle`.
- `buy` je koliko banka plaća tebi za stranu valutu. `sell` je koliko ti banka naplaćuje.
- `transfer` su devize (kartica, prijenos), a `cash` je efektiva (gotovina). Može biti `null`.
- `middle_source` je `published` (banka objavila) ili `computed_avg_transfer` (izračunato kao prosjek transfer buy/sell; vijetnamske banke ne objavljuju srednji tečaj).
- JPY/IDR i slični: skripta je već preračunala na 1 jedinicu.
- `global.json` → `global_reference.<izvor>`: jedinice valute za **1 EUR** (srednji). Izvori su `ecb` (30 valuta, samo radnim danima), `open_er_api` (166), `fawazahmed0` (163).

## 4. Struktura datoteka

`rates/index.json`:
```json
{
  "schema_version": 1,
  "generated_at": "UTC",
  "checked_at": "UTC",
  "global_checked_at": "UTC",
  "countries": {
    "HR": {
      "name": "Hrvatska", "name_en": "Croatia", "local_currency": "EUR", "file": "HR.json",
      "checked_at": "UTC", "next_refresh_at": "UTC",
      "sources": {"hnb": "ok", "pbz": "ok", "otp": "error", "erste": "skipped"}
    }
  }
}
```

`rates/<ISO2>.json`:
```json
{
  "country": "VN", "name": "Vijetnam", "local_currency": "VND", "checked_at": "UTC",
  "central_bank": { "...isti oblik kao banka..." },
  "banks": [
    {
      "id": "vietcombank", "name": "...", "kind": "bank",
      "url": "endpoint", "page_url": "javna stranica tečajne liste (za poveznicu 'izvor')",
      "status": "ok | stale | error | skipped",
      "fetched_at": "UTC", "valid_for": "YYYY-MM-DD", "published_at": "lokalno s offsetom",
      "fetched_via": "github | cloudflare | box",
      "middle_source": "published | computed_avg_transfer",
      "rates": {
        "EUR": {"middle": 29222.01, "transfer": {"buy": 28617.9, "sell": 29826.12}, "cash": {"buy": 28331.73, "sell": 29826.12}}
      },
      "stale_since": "samo ako je stale", "last_error": "samo ako je stale/error"
    }
  ]
}
```

Statusi:
- `ok`: svježe.
- `stale`: zadnji dohvat nije uspio, pa su prikazani zadnji dobri podaci. Prikaži "zadnje provjereno <fetched_at>". Podaci s Workera/servera stariji od 36 h automatski postaju `stale`.
- `error`: nema podataka.
- `skipped`: izvor je namjerno isključen. U UI ga sakrij.

Sva vremena su u UTC. U appu ih prikaži u lokalnom vremenu korisnika.

## 5. Kada se što osvježava

- Workflow ide svaki sat, ali država se dohvaća samo u svojim **lokalnim jutarnjim/dnevnim terminima** (npr. HR 07, 09, 12, 18 lokalno; VN/KH/LA 07, 09, 12, 18 ICT; ID i 10:30 WIB za JISDOR; TH 07 i 18:30; TR i 15:45 za TCMB). Točno je u `schedule_config.json`.
- Vikendom većina država samo jednom (07 lokalno). Banke vikendom ne mijenjaju liste.
- Ako GitHub preskoči pokretanje, sljedeće pokretanje nadoknadi.
- VietinBank, ABA i Bank of Ceylon dohvaća Cloudflare Worker (dvaput dnevno po lokalnom vremenu), a moj server je rezerva.
- `index.json` → `next_refresh_at` je kad se otvara sljedeći termin. Stvarni dohvat bude do ~1 h kasnije.

## 6. Kako app treba raditi s podacima

1. Pri pokretanju, i svakih ~60 min dok je online, dohvati `rates/index.json`. Zatim dohvati `rates/<ISO2>.json` samo za odabrane države i to samo ako je njihov `checked_at` noviji od spremljenog.
2. Sve spremi u IndexedDB/Cache za offline rad.
3. Ako GitHub Pages ne odgovori, koristi jsDelivr URL.
4. Za državu bez banaka koristi `central_bank`. Ako ni njega nema, izračunaj iz `global.json` (cross rate preko EUR).
5. Glavni ekran: veliki iznos po srednjem tečaju odabrane banke. Sitnije ispod: kupovni/prodajni (transfer i cash) i usporedba odabranih banaka.
6. Ispod rezultata malim slovima: "AI bot provjerio: <checked_at države>" + poveznica `page_url` izvora.
7. Ako je `checked_at` stariji od 24 h, ili je banka `stale`, prikaži malu nenametljivu oznaku.
8. Ručni tečaj kartice: korisnik upiše "platio X lokalne, skinuto Y EUR". App izračuna efektivni tečaj i usporedi ga sa srednjim (% razlike). Spremi ga kao preferirani tečaj, s datumom.
9. Atribucija u appu (obavezno): "Rates By Exchange Rate API" (open.er-api.com) + napomena "Tečajevi banaka su informativni."

## 7. Pravila za buduće izmjene backenda

- Nove banke: dodati adapter u `fetch_rates.py` + unos u `sources_config.json` + termine u `schedule_config.json`. Koristiti službene JSON/XML/TXT endpointe, poštivati robots.txt i ne zaobilaziti zaštite od botova.
- Ako banka blokira GitHub, ide u Cloudflare Worker (`cloudflare/worker.js`).
- Tajne: Bank of Thailand token je GitHub secret `BOT_TH_TOKEN`. Ne smije se nikad stavljati u kod ni JSON.
- Shema je `schema_version: 1`. Kod promjene treba povećati verziju i ostaviti stara polja.
