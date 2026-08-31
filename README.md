# MarketLens — pipeline automat de inteligență financiară

Colectează știri financiare din multiple surse (RSS, API, Google News),
le procesează printr-un pipeline de 25+ module (detectare companii,
sentiment, impact, încredere), și generează automat un raport
BUY/SELL/HOLD, actualizat zilnic, fără intervenție manuală.

Acesta e echivalentul "de producție" al notebook-ului Colab pe care
l-am folosit în dezvoltare — aceleași module, aceeași logică, testate
identic, dar rulează **singur, automat, în fiecare zi**, prin GitHub
Actions, în loc să ceară apăsarea manuală de "Run all".

## Cum funcționează

- **`.github/workflows/daily.yml`** — rulează în fiecare zi la 07:00
  UTC: colectează știri, le procesează, actualizează recomandările,
  regenerează raportul.
- **`.github/workflows/weekly_backfill.yml`** — rulează duminica la
  06:00 UTC: caută pe Google News istoricul (60 zile) pentru toate
  companiile urmărite, apoi rulează și pipeline-ul zilnic.
- **`.github/workflows/tests.yml`** — rulează toată suita de teste la
  fiecare modificare, ca o eroare introdusă din greșeală să fie
  prinsă înainte să ajungă să ruleze automat.
- **`data/marketlens.db`** — baza de date SQLite (articole +
  recomandări), actualizată și salvată automat de fiecare rulare, în
  chiar acest repository (nu mai e nevoie de Google Drive).
- **`docs/index.html`** — raportul Dashboard, regenerat la fiecare
  rulare. Cu GitHub Pages activat (vezi pașii de mai jos), acesta
  devine accesibil la o adresă fixă, mereu la zi.

## Pași de configurare (o singură dată)

### 1. Încarci fișierele în repository

Descarci arhiva `.zip` primită, o dezarhivezi, și încarci **tot
conținutul** (păstrând structura de foldere) în repository-ul creat
pe GitHub. Cel mai simplu: prin interfața web GitHub (`Add file` →
`Upload files`, tragi toate fișierele/folderele), sau prin `git` din
linia de comandă dacă ești confortabil cu asta.

**Important:** structura de foldere trebuie păstrată exact
(`src/`, `tests/`, `data/`, `docs/`, `.github/workflows/`) — GitHub
Actions se bazează pe aceste căi exacte.

### 2. Activezi GitHub Actions (dacă nu e deja activ)

De obicei se activează automat la primul push cu fișiere `.yml` în
`.github/workflows/`. Verifici în tab-ul **Actions** al
repository-ului — dacă vezi cele 3 workflow-uri listate, e activ.

### 3. Rulezi manual prima dată (nu aștepta programarea automată)

1. Tab **Actions** → click pe **MarketLens Daily Pipeline** (în
   stânga)
2. Buton **Run workflow** (dreapta) → **Run workflow** din nou, ca
   să confirmi
3. Așteaptă 1-2 minute, urmărește progresul live (click pe rularea
   care apare)
4. Dacă vezi ✅ verde la final — a mers. Dacă ❌ roșu — click pe
   rulare, vezi exact la ce pas a eșuat (mesajele de eroare sunt
   afișate direct acolo)

### 4. Activezi GitHub Pages (pentru link public la Dashboard)

1. **Settings** (tab-ul repository-ului) → **Pages** (meniul din
   stânga)
2. **Source**: `Deploy from a branch`
3. **Branch**: `main`, folder: `/docs`
4. **Save**

După câteva minute, raportul devine accesibil la:
`https://<numele-tau-de-utilizator>.github.io/<numele-repo>/`

**Notă despre vizibilitate:** GitHub Pages gratuit necesită
repository **public** — codul (și datele acumulate) devin vizibile
oricui. Dacă vrei totul privat mai târziu, ai nevoie de GitHub Pro
(~4$/lună) sau muți afișarea în altă parte.

### 5. (Opțional) Rulezi și backfill-ul săptămânal manual, prima dată

La fel ca la pasul 3, dar alegi **MarketLens Weekly Historical
Backfill**. Durează câteva minute (interoghează Google News pentru
fiecare companie urmărită, cu pauză politicoasă între cereri).

## Cum știi dacă ceva nu merge

GitHub trimite automat un email proprietarului repository-ului dacă o
rulare eșuează (verifică setările tale de notificări GitHub dacă nu
primești aceste emailuri). În plus, poți verifica oricând manual în
tab-ul **Actions** — fiecare rulare are un istoric complet, cu toate
mesajele afișate în consolă (inclusiv câte articole s-au colectat,
câte au trecut de fiecare filtru, etc.).

## Rulare locală (pentru testare/dezvoltare)

```bash
pip install -r requirements.txt

# Rulează pipeline-ul zilnic
python run_daily.py

# Rulează backfill-ul săptămânal + pipeline-ul
python run_weekly_backfill.py

# Rulează toată suita de teste
PYTHONPATH=src python -m pytest tests/ -q
```

## Portofoliu și risc (Faza 11)

Faza 11 adaugă nivelul dintre **semnale** și o viitoare execuție:
starea portofoliului, expunerea, metricile de risc, limitele și
decizia de risc.

**Nu tranzacționează nimic.** Nu există conexiune la broker, nu există
ordine, nu există credențiale. Cea mai „activă" ieșire este un rând
inert în `order_intents`, care descrie ce *ar fi* instruit dacă ar
exista un nivel de execuție. MT5 / Interactive Brokers aparțin unei
faze ulterioare.

Motorul operează **doar pe poziții declarate explicit** — nu inventează
un portofoliu. Toate prețurile vin din `price_candle_cache` (lumânări
deja stocate), niciodată dintr-un apel live, deci orice decizie poate
fi recalculată identic mai târziu.

```bash
# Declară un portofoliu și evaluează-l (fără să scrii nimic)
python scripts/evaluate_portfolio_risk.py --portfolio meu --create --cash 100000 --dry-run

# Evaluează la o ancoră istorică (replay punct-în-timp)
python scripts/evaluate_portfolio_risk.py --portfolio meu --as-of 2026-08-27T20:00:00+00:00
```

Paginile **Portofoliu** și **Risc** din Dashboard afișează rezultatul.
Fără un portofoliu declarat, ele arată explicit acest lucru — nu date
simulate.

## Backtesting (Faza 12)

Faza 12 reia **întregul lanț de decizie istoric**: informația
disponibilă la momentul T → semnal → context de portofoliu → motorul
real de risc (Faza 11) → alocare → execuție simulată pe lumânări deja
stocate → stare de portofoliu → performanță.

**Nu tranzacționează nimic.** Singurul executor implementat este
`SimulationExecutor`. `PaperExecutor` și `BrokerExecutor` (MT5 / IBKR)
aparțin unor faze ulterioare și nu există aici.

Garanțiile pe care le impune:

- **Fără look-ahead.** Fiecare citire de preț poartă `timestamp <= as_of`
  în SQL, iar o umplere trebuie să fie *strict* după ordinul ei.
  Încălcările ridică `TemporalViolation` și opresc rularea.
- **Costurile sunt reale.** Comisionul și slippage-ul sunt debitate din
  numerar la fiecare umplere, nu scăzute dintr-un randament final.
- **Riscul este cel real.** Simularea apelează `PortfolioService.evaluate()`,
  aceeași funcție ca pipeline-ul live — nu un strat simplificat.
- **Reproductibil.** Aceleași intrări, aceleași versiuni și aceeași
  amprentă de configurație produc același rezultat.

```bash
# Rulare pe semnalele reale stocate
python scripts/run_backtest.py --name prima-rulare

# Exersează mecanismul pe semnale generate (NU este dovadă despre o strategie)
python scripts/run_backtest.py --synthetic-signals --cost-sensitivity --bootstrap
```

**Atenție onestă:** toate semnalele din baza actuală sunt *suprimate*
(încredere scăzută, predicții învechite, eșantion mic), deci o rulare
reală produce zero tranzacții și raportează exact asta. Un backtest
**nu este o dovadă de profitabilitate viitoare**; pagina Backtesting
afișează scorul de calitate a cercetării (care *nu* este un scor de
profitabilitate) alături de toate limitările declarate.

## Structura proiectului

```
├── src/                        # modulele pipeline-ului, testate individual
│   ├── backtest/               # Faza 12 — replay istoric, execuție simulată
│   ├── portfolio/              # Faza 11 — portofoliu, expunere, risc
│   ├── signals/                # Faza 10 — motorul de semnale
│   ├── domain/                 # modelele canonice de domeniu
│   └── data_access/            # scheme SQL + repository-uri
├── tests/                      # suita completă de teste (2000+ teste)
├── data/marketlens.db          # baza de date persistentă (creată la prima rulare)
├── docs/index.html             # MarketLens Terminal (regenerat la fiecare rulare)
├── run_daily.py                # script de orchestrare — rularea zilnică
├── run_weekly_backfill.py      # script de orchestrare — backfill istoric săptămânal
├── scripts/                    # rulări manuale per fază
├── requirements.txt
└── .github/workflows/          # workflow-urile GitHub Actions
```

## Disclaimer

Acest raport e generat automat, pe baza analizei de sentiment din
știri publice. **Nu constituie sfat financiar.** Verifică întotdeauna
independent înainte de orice decizie de investiție.
