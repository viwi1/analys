# Användarguide – Kylkompressoranalys

Scriptet analyserar kompressordata från IWMAC-export och beräknar **tillgänglig kylkapacitet** – hur mycket effekt som finns kvar för ny last utöver befintlig belastning. Stödjer flera systemtyper: **MT** (C1–C8), **Frys** (F1–F8), **Komfort** (H1–H4).

## Syfte

Scriptet hjälper dig att:

- Uppskatta aktuell kylbelastning över tid
- Beräkna tillgänglig kapacitet (marginal för ny last)
- Se tillgänglig kapacitet per steg (C1, C1+C2, …)
- Se hur ofta systemet går nära eller vid full last
- Planera utrymme för t.ex. nya kylbehov

## Installation

### Förutsättningar

- Python 3.9 eller senare
- pip

### Steg 1: Skapa virtuell miljö (rekommenderat)

```bash
cd refrigeration-analysis
python3 -m venv .venv
```

### Steg 2: Installera beroenden

```bash
.venv/bin/pip install -r requirements.txt
```

(Windows: `.venv\Scripts\pip install -r requirements.txt`)

### Steg 3: Verifiera installationen

```bash
.venv/bin/python analyze_refrigeration.py --help
```

## Streamlit (webbgränssnitt)

För ett interaktivt webbgränssnitt, kör:

```bash
streamlit run app.py
```

(Windows med venv: `.venv\Scripts\streamlit run app.py`)

Streamlit öppnas på http://localhost:8501. Du kan då:
- Justera samplingsintervall och histogramintervall i sidofältet
- Köra analys med en knapp
- Visa resultat, grafer och ladda ner PDF-rapporten

Konfiguration för Streamlit finns i `.streamlit/config.toml` (port, tema m.m.).

## Konfiguration

Alla inställningar görs i `config.json`. Varje kompressor har:

| Fält         | Beskrivning                                                                 |
|--------------|-----------------------------------------------------------------------------|
| `name`       | C1–C8 (MT), F1–F8 (Frys), H1–H4 (Komfort). Prefix avgör system.            |
| `type`       | Valfritt: `"inverter"`, `"onoff"` eller `"auto"` (standard – identifieras från data) |
| `capacity_kw`| **Effekt i kW** – används för last- och tillgänglighetsberäkning           |
| `file`       | Filnamn i input-mappen (t.ex. hash eller C1.csv)                           |
| `columns`    | Valfritt: `[timestamp_kolumn, värdekolumn]` (0-indexerade) – krävs om flera kompressorer delar samma fil |

**Systemidentifiering:** C → MT, F → Frys, H → Komfort. Varje system analyseras separat.

**Kompressortyp:** Med `type: "auto"` identifieras inverter (0–100 %) eller on/off (0/1) automatiskt. C1 är alltid inverter.

### Exempel config.json

```json
{
  "compressors": [
    {
      "name": "C1",
      "type": "inverter",
      "capacity_kw": 38,
      "file": "1675af4a8ed57c2bd2ae3b5b1daab234.csv"
    },
    {
      "name": "C2",
      "type": "onoff",
      "capacity_kw": 17,
      "file": "1881b54df468e0a814ef9848e231715b.csv",
      "columns": [0, 1]
    }
  ]
}
```

**Effekt per kompressor** sätts i `capacity_kw` – det är den effekt (kW) som kompressorn bidrar med vid full drift.

## Input-filer

### Export från IWMAC

Exportera CSV-loggar från IWMAC enligt det format som scriptet förväntar sig:

**Inverter-fil**

- Rad 1: metadata (hoppas över)
- Data: kolumn 0 = tidsstämpel, kolumn 1 = värde (0–100 %)

**On/off-fil**

- Kan innehålla flera kompressorer i samma fil
- Varje serie: `timestamp, värde` (0 eller 1)
- Varje rad = tillståndsändring (på/av)
- Använd `columns` i config för att peka ut rätt kolumner

### Placering

Lägg CSV-filerna i mappen `input/` – eller ange annan mapp med `--input`.

## Hur beräkningen går till

### Signaler per kompressor

Varje kompressor har en signal som anger hur mycket den kör:

| Typ      | Råvärde i logg | Omvandling till bidrag        |
|----------|----------------|-------------------------------|
| Inverter | 0–100 %        | `signal = värde / 100` (0–1)  |
| On/off   | 0 eller 1      | `signal = värde` (0 eller 1)  |

### Momentan last

Per tidssteg beräknas lasten som summan av varje kompressors bidrag:

```
load_kw = Σ (signal × capacity_kw)
```

Exempel med C1 (inverter 80 %, 38 kW), C2 (på, 17 kW), C3 och C4 (av, 17 + 18 kW):

```
load_kw = 0,80 × 38 + 1 × 17 + 0 × 17 + 0 × 18 = 47,4 kW
```

### Total kapacitet och tillgänglig kapacitet

- **Total kapacitet:** `Q_max = Σ capacity_kw` (summan av alla kompressorers effekt)
- **Tillgänglig kapacitet (vid varje tidssteg):**  
  `available_kw = Q_max - load_kw`
- **Lastandel:**  
  `load_fraction = load_kw / Q_max` (0–1)

### Uppskattad praktisk tillgänglig effekt

`Q_max - load_95`, där `load_95` är 95-percentilen av lasten. Detta beskriver ungefär hur mycket effekt som brukar finnas kvar för ny last.

### Tidsbaserade statistik

Tidsintervallen mellan loggade punkter beräknas, och varje rad viktas med sitt intervall (timmar):

- **Tid över 90 % last:** `load_fraction >= 0,90`
- **Tid över 80 % last:** `load_fraction >= 0,80`
- **Tid vid full last:** `load_fraction >= 0,99`

Timmar i datamängden räknas och extrapoleras till **timmar per år** (8760 h / antal timmar i datamängden).

### Histogram

Lasten grupperas i intervall om 10 kW (0–10, 10–20, …). Varje tidssteg bidrar med sitt intervall (timmar) till rätt intervall. Låg last betyder hög tillgänglig kapacitet.

### Veckografer

Per vecka beräknas och ritas:

- **95-percentil last** (huvudstapel) – lasten som bara överskrids 5 % av tiden under veckan; ger en typisk toppbild
- **Max last** (tunn stapel) – eventuella spikar; en ensam datapunkt kan inte få hela veckan att se maxad ut
- **5-percentil tillgänglig** (huvudstapel) – 95 % av tiden finns minst denna tillgängliga kapacitet
- **Min tillgänglig** (tunn stapel) – värsta tillfället under veckan

En horisontell linje visar installerad effekt (Q_max). Percentilerna ger en mer realistisk bild än enbart max/min.

### Tillgänglig kapacitet per steg

Stegen C1, C1+C2, C1+C2+C3, … visar den ackumulerade kapaciteten och den kvarvarande tillgängliga effekten per driftsteg.

## Körning

### Grundläggande

```bash
python analyze_refrigeration.py
```

Läser `config.json`, CSV från `input/` och skriver till `output/`.

### Flaggor

| Flagga       | Kort | Beskrivning                                |
|--------------|------|--------------------------------------------|
| `--input`    | `-i` | Mapp med CSV-filer (default: `input`)      |
| `--output`   | `-o` | Output-mapp (default: `output`)            |
| `--config`   | `-c` | Sökväg till config.json                    |

### Exempel

```bash
# CSV-filerna ligger i mappen python/
python analyze_refrigeration.py --input python/

# Ange både input och output
python analyze_refrigeration.py -i ../data/csv -o ../resultat
```

## Tolkning av resultat

### Output-struktur

Resultat delas upp per system:

```
output/
  rapport.pdf         Samlad PDF-rapport (alla system)
  MT/                 (C1–C8, kyl/MT)
    summary.txt
    timeseries_with_load.csv
    histogram_load.csv
    histogram_load.png
    weekly_load_available.png
  Frys/               (F1–F8)
    ...
  Komfort/            (H1–H4)
    ...
```

### summary.txt

Resultatet leder med **tillgänglig kapacitet**:

- **Uppskattad praktisk tillgänglig effekt** – typiskt hur mycket kW som finns kvar (total kapacitet minus 95-percentil last)
- **Medel tillgänglig kapacitet** – genomsnittlig marginal
- **Tid över 90 % last** – hur ofta systemet är nära max
- **Tid vid full last** – timmar där belastningen ≥ 99 % (även timmar/år)
- **Tid över 80 % last** – timmar där belastningen överstiger 80 % av total kapacitet

Längre ner finns belastningsstatistik, tillgänglig kapacitet per steg (C1, C1+C2, …) och driftdata per kompressor.

### timeseries_with_load.csv

Tidsserie med:

- `timestamp`
- En kolumn per kompressor (t.ex. C1, C2, …)
- `load_kw` – momentan last
- `available_kw` – tillgänglig kapacitet vid varje tidpunkt

### histogram_load.csv / histogram_load.png

Visar hur många timmar systemet ligger i olika lastintervall. Låg last innebär hög tillgänglig kapacitet.
