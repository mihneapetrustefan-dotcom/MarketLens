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
exista un nivel de execuție. Execuția apare abia în Faza 13 (paper) și
Faza 15 (Interactive Brokers, tot paper).

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

**Nu tranzacționează nimic.** Singurul executor din această fază este
`SimulationExecutor`. `PaperExecutor` (Faza 13) și adaptorul IBKR
(Faza 15) au venit ulterior, pe aceeași interfață.

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

## Paper trading (Faza 13)

Faza 13 rulează **exact același lanț ca producția** — semnal → motorul
real de risc (Faza 11) → alocare → intenție de ordin — și diferă
într-un singur punct: executorul, care simulează umplerea în loc să
trimită ordinul undeva.

**Nu plasează niciun ordin real.** Executorul din această fază nu are
conexiune la niciun broker, nu există credențiale și niciun cont real
nu e citit sau modificat. `ExecutionVenue`
are un singur membru, `PAPER`, iar `PaperAccount.is_paper` nu poate fi
setat pe `False` — construcția eșuează. Nu e o cale dezactivată, e o
cale absentă.

### Nu e un daemon, și asta e deliberat

Repozitoriul nu are runtime persistent — fiecare fază rulează ca job
programat. Deci sesiunea e **durabilă**, nu rezidentă: starea ei trăiește
în baza de date, `tick()` o avansează, iar apelantul decide cadența. Un
`--ticks 1` programat zilnic *este* un cont de paper care merge continuu.

Ce ține construcția asta onestă e monitorul de prospețime: la fiecare
tick se măsoară cât de vechi sunt barele pe care se decide și se
raportează exact asta. O sesiune care lucrează pe bare de acum patru
zile spune că lucrează pe bare de acum patru zile — nu le prezintă ca
fiind live.

Garanțiile pe care le impune:

- **Riscul nu poate fi ocolit.** Ordinele se creează *numai* din
  `evaluation.intents`, iar intențiile există doar când
  `PortfolioService.evaluate()` a întors o decizie aprobatoare. Nu
  există drum de la semnal la ordin care să nu treacă prin același
  apel al Fazei 11 pe care îl face pipeline-ul.
- **Idempotență.** Fiecare ordin poartă o cheie derivată din intrările
  care l-au decis, deci un tick re-rulat (workflow reluat, repornire la
  mijloc, invocare dublă) își recunoaște propria muncă în loc să dubleze
  poziția.
- **Recuperare.** Un backtest care crapă se re-rulează; o sesiune de
  paper nu poate — timpul a trecut deja peste ticks-urile procesate.
  Checkpoint-urile plus istoricul de umpleri reconstruiesc starea de
  unde a rămas.
- **Aceleași costuri ca la backtesting.** Modelele de cost și slippage
  ale Fazei 12 sunt refolosite, versionate; o divergență între cele două
  faze e o eroare de configurare, nu o observație de piață.
- **Reconciliere care nu repară în tăcere.** Cinci verificări la fiecare
  tick compară starea păstrată cu ce spun execuțiile. O neconcordanță
  intră în safe mode și e raportată, nu corectată pe ascuns.

```bash
# Avansează un tick pe semnalele reale stocate
python scripts/run_paper_session.py --name prima-sesiune --ticks 1

# Reia sesiunea după o oprire, apoi avansează
python scripts/run_paper_session.py --resume --ticks 1

# Exersează mecanismul pe semnale generate (NU este dovadă despre o strategie)
python scripts/run_paper_session.py --replay --days 120 --synthetic-signals

# Controale operaționale
python scripts/run_paper_session.py --status
python scripts/run_paper_session.py --pause
python scripts/run_paper_session.py --emergency-stop
```

**Atenție onestă:** ca și la backtesting, toate semnalele din baza
actuală sunt *suprimate*, deci o sesiune reală produce zero ordine și
raportează exact asta. Iar o perioadă scurtă de paper trading **nu
stabilește nimic** despre o strategie: comparația cu backtestul tratează
metricile mecanice (rata de umplere, slippage, respingeri) ca
diagnostice, dar randamentul e raportat explicit ca ne-concluziv.

Workspace-ul **Paper trading** din dashboard poartă permanent un banner
`PAPER MODE`, pentru că o pagină cu curbă de capital, poziții și execuții
fără eticheta aia arată, la o privire rapidă, exact ca una despre un
cont real.

## Abstractie de broker si executie (Faza 14)

Faza 14 construieste **granita**, nu un broker. Nucleul se opreste la
`OrderIntent`; sub el, un singur adaptor per loc de executie traduce
catre si dinspre tipurile canonice.

```
SEMNAL -> PORTOFOLIU -> RISC -> ORDER INTENT
                                     |
========== granita neutra fata de broker ==========
                                     |
                        EXECUTION ORCHESTRATOR
                                     |
                        BROKER GATEWAY (abstract)
                                     |
                        BROKER ADAPTER (concret)
                                     |
                             BROKER EXTERN
```

**Nu exista executie cu bani reali.** Interactive Brokers e singurul
broker al proiectului si e conectat exclusiv in mediul PAPER. Niciun
adaptor nu accepta un mediu cu bani reali, aplicatia nu detine
credentiale si niciun cont real nu e citit sau modificat. Calea nu e
dezactivata — lipseste din cod, iar asta e impus in cinci locuri
independente:

1. `ExecutionEnvironment.LIVE` nu poate fi atasat unui `Broker` care se
   declara implementat, nici unui `BrokerAccount` deloc — tipurile
   refuza constructia.
2. `ExecutionSafety.allow_real_orders` e o proprietate **fara setter**.
3. Poarta de siguranta refuza `LIVE` inaintea oricarei alte verificari.
4. Nicio permisiune nu acorda executie reala — `LIVE_EXECUTION_ADMIN` e
   refuzata chiar si cand e detinuta.
5. Niciun adaptor capabil de un ordin real nu exista.

Variabila `MARKETLENS_ALLOW_REAL_ORDERS` e *citita* — dar numai ca
sistemul sa poata spune ca cineva a setat-o. Nu schimba nimic.

### Ce garanteaza stratul

- **Ordinea operatiilor e modelul de siguranta.** Siguranta, rutare,
  idempotenta, corespondenta, capabilitate, sesiune de piata, risc,
  validare — toate inaintea liniei. Sub linie: un singur pas care poate
  ajunge la un loc de executie.
- **Timeout-ul nu e esec.** O submisie care expira devine `UNKNOWN`,
  niciodata `FAILED`, si **nu se retrimite niciodata** — brokerul poate
  sa o fi acceptat deja. Se rezolva intrebandu-l.
- **Idempotenta e derivata, nu atribuita.** Cheia se recalculeaza din
  decizie, deci o reluare dupa un crash isi recunoaste propria munca.
  Id-ul semnalului e deliberat exclus (lectia Fazei 13).
- **Reconcilierea raporteaza, nu repara.** Starea interna e o *credinta*
  despre broker; brokerul e faptul. Nicio neconcordanta nu se corecteaza
  in tacere.
- **Evenimentele pot veni duplicate, tarziu si in dezordine.** Toate trei
  sunt normale, si fiecare corupe starea altfel daca e tratata naiv.

```bash
# Stare: brokeri, conturi, comutatoare de siguranta
python scripts/run_execution.py

# Valideaza un ordin si opreste-te inainte de submisie
python scripts/run_execution.py --dry-run-order --quantity 25

# Trimite catre PAPER (nevoie de permisiune explicita)
python scripts/run_execution.py --submit --allow-paper --assume-risk-approved --fill

# Reconciliaza si urmareste lantul complet al unui ordin
python scripts/run_execution.py --reconcile
python scripts/run_execution.py --trace <order_id>

# Oprire de urgenta
python scripts/run_execution.py --kill-switch on --reason "..." --allow-paper
```

Detaliile complete — masina de stari, cele sase identificatoare,
ordonarea evenimentelor, modelul de capabilitati si ce trebuie sa faca
Fazele 15 si 16 — sunt in
[docs/execution-architecture.md](docs/execution-architecture.md).

## Interactive Brokers (Faza 15)

IBKR ca **adaptor in spatele granitei Fazei 14**. Nimic deasupra
granitei nu stie ca IBKR exista — nucleul, semnalele, portofoliul si
motorul de risc n-au fost atinse.

```
ORDER INTENT
     |
=== granita neutra fata de broker (Faza 14) ===
     |
EXECUTION ORCHESTRATOR -> BROKER GATEWAY
                               |
                    +----------+-----------+
                    |          |           |
                 PAPER      IBKR      DISABLED
                               |
                        IBKR TRANSPORT     <- a doua granita
                               |
                    Client Portal Web API
```

**Doar paper. Nicio executie cu bani reali.** `IBKR_ENVIRONMENT`
accepta exclusiv `paper`, configuratia refuza orice altceva la
construire, gateway-ul refuza sa fie construit pentru un mediu cu bani
reali, iar stratul de siguranta al Fazei 14 refuza `LIVE` inainte de
toate.

### Transportul ales: Client Portal Web API

Ambele interfete IBKR cer un program Java local pe care il autentifici
tu. Am ales Client Portal pentru patru motive specifice acestui
repozitoriu:

1. **Se potriveste cu forma pe care Faza 14 o are deja** — cerere si
   raspuns, cu `poll_events`. TWS API e o magistrala asincrona de
   callback-uri care ar cere un runtime persistent; proiectul nu are.
2. **Zero dependinte noi** — `requests` exista deja. `ibapi` ar trebui
   adus manual din arhiva IBKR.
3. **Ruleaza pe Linux**, unde ruleaza si restul proiectului.
4. **`conid` e un concept REST de prim rang**, si se aseaza direct
   peste `BrokerInstrumentMapping`.

Decisiv pentru securitate: **gateway-ul detine credentialul, deci
aplicatia nu-l vede niciodata.** Nu exista camp de utilizator sau
parola nicaieri in Faza 15, iar `.env.example` explica de ce adaugarea
lui ar fi o greseala.

### Doua porti inainte de orice ordin

Conectarea **nu** e permisiune de a tranzactiona:

```
IBKR_ENABLED=true                    # integrarea e pornita
IBKR_PAPER_ORDERING_ENABLED=false    # a doua poarta, inchisa implicit
```

### Ce refuza adaptorul sa ghiceasca

- **`ticker == instrument` e fals la IBKR.** Acelasi simbol e listat pe
  mai multe locuri, in mai multe monede. Cand raman mai multe
  contracte, raspunsul e o **ambiguitate structurata**, nu o alegere.
  Nimic nu se salveaza si nimic nu tranzactioneaza.
- **Un timeout nu e un esec.** Devine `UNKNOWN`, niciodata `FAILED`, si
  **nu se retrimite niciodata** — se rezolva intrebandu-l pe IBKR.
- **`Inactive` nu e terminal.** E categoria-cos a IBKR pentru un ordin
  pe care il tine dar nu il lucreaza; citit ca anulat ar lasa un ordin
  viu pe care sistemul il crede inchis.
- **Executiile se deduplica pe id-ul de executie IBKR**, niciodata pe
  campurile vizibile: doua executii diferite pot fi identice in tot
  restul.

```bash
# Totul, fara gateway, fara cont, fara retea
python scripts/run_ibkr.py --mock

# Cu un Client Portal Gateway pornit si autentificat de tine
python scripts/run_ibkr.py --status
python scripts/run_ibkr.py --account-info
python scripts/run_ibkr.py --resolve --symbol AAPL --instrument i-aapl
python scripts/run_ibkr.py --dry-run-order --instrument i-aapl --quantity 1

# Ordin paper (necesita confirmarea explicita a portii)
python scripts/run_ibkr.py --submit --instrument i-aapl --quantity 1 \
    --assume-risk-approved --allow-paper-orders

python scripts/run_ibkr.py --reconcile
python scripts/run_ibkr.py --resolve-unknown
```

**Nu a fost validat pe un cont IBKR real.** Nu exista cont, gateway sau
credential in mediul in care a fost construit. Toate testele ruleaza pe
un dublu determinist, iar formele de endpoint vin din documentatia
publicata, nu din trafic observat. Diferenta e reala si e enumerata in
[docs/PHASE_15_IBKR_ARCHITECTURE.md](docs/PHASE_15_IBKR_ARCHITECTURE.md);
procedura care o inchide e in
[docs/PHASE_15_IBKR_RUNBOOK.md](docs/PHASE_15_IBKR_RUNBOOK.md).

## Pregatire de productie si guvernanta executiei (Faza 16)

Fazele 11-15 au construit lantul: decizie de risc -> intentie de ordin
-> ordin validat -> ordin IBKR paper -> umplere -> pozitie. Functioneaza
— si exact in acel moment intrebarea interesanta nu mai e *poate
executa*, ci *in ce conditii are voie*.

Faza 16 raspunde la asta. Adauga stratul dintre **capabil** si
**permis**.

**Interactive Brokers este singurul broker al proiectului.** Nu exista
adaptor MetaTrader 5, nu exista strat de compatibilitate MT5, nu exista
rutare intre brokeri si nu exista rezervat loc pentru un al doilea loc
de executie.

- **Niveluri de executie** (0-7), de la cercetare la productie. Toate
  nivelurile cu bani reali (5-7) sunt specificate, controlate prin
  porti — si **niciunul nu e implementat**. Aprobarea nivelului 7 se
  inregistreaza, iar nivelul efectiv ramane 3, pentru ca implementarea
  e un fapt despre cod pe care nicio aprobare nu-l schimba.
- **Porti de promovare** — 14 criterii masurabile. O poarta nemasurata
  **blocheaza**: faptul ca nu ai masurat ceva nu e dovada ca e in
  regula. Profitabilitatea nu e o poarta, deliberat — o strategie poate
  fi profitabila pe termen scurt din noroc.
- **Aprobare umana** — nimeni nu-si aproba propria cerere. Aprobarile
  expira, pentru ca o permisie pe care nimeni n-o reinnoieste e felul in
  care o decizie temporara devine permanenta din neatentie.
- **Sesiuni de tranzactionare** — executia se intampla doar intr-o
  sesiune deschisa explicit, cu configuratie inghetata si amprentata.
  O verificare de preflight nemasurata blocheaza la fel de ferm ca una
  esuata. Pauza e reversibila; oprirea de urgenta e terminala.
- **Limite operationale** — 23 de verificari intr-o singura trecere,
  toate esuand inchis. Niciun plafon pentru bani reali nu vine cu o
  valoare implicita: un numar livrat ca default devine default de
  productie din neatentie. Limitele de pierdere se **zavorasc** — nu se
  sterg cand piata revine.
- **Sanatate pe capabilitati** — noua, masurate separat. Agregatul e
  **cea mai proasta** citire, niciodata o medie. Doar `HEALTHY` permite
  ordine noi; `DEGRADED` nu.
- **Lantul cauzal complet per tranzactie** — 21 de identificatori,
  stocati plat, plus semnalele care **nu** au devenit tranzactii si cine
  le-a oprit: sistemul sau piata. Un sistem care inregistreaza doar ce a
  facut nu poate distinge un semnal prost de unul bun pe care riscul l-a
  oprit.

**Executia cu bani reali e blocata, iar blocajul e o absenta.** Nu e un
comutator inchis: niciun adaptor nu accepta un mediu cu bani reali, deci
nu exista cale de oprit. Sase locuri independente impun asta, iar
`ExecutionSafety.allow_real_orders` e o proprietate fara setter — nicio
cale de cod nu o poate porni si nicio variabila de mediu nu e citita.

Ce **nu** exista, deliberat: auto-invatare, invatare prin recompensa,
modificare autonoma de strategie, promovare autonoma de model,
gestionare autonoma de capital. Ce exista e **lineage-ul** de care un
sistem de invatare de mai tarziu ar avea nevoie — inregistrat acum, cat
timp tranzactiile se intampla, pentru ca nu poate fi reconstruit dupa.

```bash
# Stare completa: nivel, sanatate, limite, sesiune
python scripts/run_operations.py --status --mock

# Pregatire si porti
python scripts/run_operations.py --readiness --mock
python scripts/run_operations.py --gates --level 5 --mock

# Promovare cu patru ochi (actori diferiti)
python scripts/run_operations.py --request-level 3 --actor alice --reason "..."
python scripts/run_operations.py --approve <request_id> --actor bob

# Sesiune
python scripts/run_operations.py --start-session --actor alice --capital-limit 25000
python scripts/run_operations.py --pause --actor alice --reason "..."
python scripts/run_operations.py --emergency-stop --actor alice --reason "..."

# Raportare
python scripts/run_operations.py --daily-report
python scripts/run_operations.py --compare
```

Arhitectura si deciziile sunt in
[docs/PHASE_16_IBKR_PRODUCTION_READINESS.md](docs/PHASE_16_IBKR_PRODUCTION_READINESS.md);
cele 18 proceduri de operare, in
[docs/PHASE_16_OPERATIONS_RUNBOOK.md](docs/PHASE_16_OPERATIONS_RUNBOOK.md).

## Structura proiectului

```
├── src/                        # modulele pipeline-ului, testate individual
│   ├── execution/              # Faza 14 — abstracție de broker, orchestrator
│   │   ├── governance.py       # Faza 16 — niveluri, porți, aprobare, pregătire
│   │   ├── limits.py           # Faza 16 — capital, pierdere, prospețime, calitate
│   │   ├── session.py          # Faza 16 — sesiuni, preflight, configurație înghețată
│   │   ├── monitoring.py       # Faza 16 — capabilități, sănătate, metrici, alerte
│   │   ├── outcomes.py         # Faza 16 — lineage, calitate, rezultate, ratări
│   │   └── adapters/           #   un adaptor per loc de execuție
│   │       └── ibkr/           # Faza 15 — Interactive Brokers (paper)
│   ├── paper/                  # Faza 13 — paper trading, ceas, prospețime, sănătate
│   ├── backtest/               # Faza 12 — replay istoric, execuție simulată
│   ├── portfolio/              # Faza 11 — portofoliu, expunere, risc
│   ├── signals/                # Faza 10 — motorul de semnale
│   ├── domain/                 # modelele canonice de domeniu
│   └── data_access/            # scheme SQL + repository-uri
├── tests/                      # suita completă de teste (2000+ teste)
├── data/marketlens.db          # baza de date persistentă (creată la prima rulare)
├── docs/index.html             # MarketLens Terminal (regenerat la fiecare rulare)
├── docs/execution-architecture.md  # Faza 14 — arhitectura de execuție
├── docs/PHASE_15_IBKR_ARCHITECTURE.md  # Faza 15 — integrarea IBKR
├── docs/PHASE_15_IBKR_RUNBOOK.md   # Faza 15 — proceduri operaționale
├── docs/PHASE_16_IBKR_PRODUCTION_READINESS.md  # Faza 16 — guvernanță și pregătire
├── docs/PHASE_16_OPERATIONS_RUNBOOK.md  # Faza 16 — cele 18 proceduri de operare
├── .env.example                # variabilele de mediu (doar substituenți)
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
