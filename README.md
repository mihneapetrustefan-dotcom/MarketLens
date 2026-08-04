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
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Structura proiectului

```
├── src/                        # toate cele 31 de module, testate individual
├── tests/                      # suita completă de teste (25 fișiere, 400+ teste)
├── data/marketlens.db          # baza de date persistentă (creată la prima rulare)
├── docs/index.html             # raportul Dashboard (regenerat la fiecare rulare)
├── run_daily.py                # script de orchestrare — rularea zilnică
├── run_weekly_backfill.py      # script de orchestrare — backfill istoric săptămânal
├── requirements.txt
└── .github/workflows/          # cele 3 workflow-uri GitHub Actions
```

## Disclaimer

Acest raport e generat automat, pe baza analizei de sentiment din
știri publice. **Nu constituie sfat financiar.** Verifică întotdeauna
independent înainte de orice decizie de investiție.
