# snapmem – Snapchat Memories für iCloud & Co. aufbereiten

`snapmem` verarbeitet einen **Snapchat-Datenexport ("Meine Daten" → Memories)**, so wie Snapchat ihn als
mehrere Zip-Dateien liefert, und erzeugt daraus einen Ordner mit fertigen Fotos und Videos:

- **Overlays eingebrannt**: Text, Sticker, Zeichnungen, Geofilter und Lens-Effekte, die Snapchat als separate
  Datei exportiert, werden mit dem Foto/Video zusammengeführt.
- **Korrekte Metadaten**: Aufnahmezeitpunkt (mit Zeitzone des Aufnahmeorts) und GPS-Standort werden so
  geschrieben, dass **Apple Fotos / iCloud** (und Google Fotos, Windows Fotos, …) sie richtig einlesen.
- **Aufgeräumt**: doppelt gespeicherte Memories werden nur einmal ausgegeben, Unstimmigkeiten werden
  protokolliert, unklare Fälle entscheidest du in einer kleinen Web-Oberfläche mit Karte.

Alles läuft lokal. Die Zips werden direkt gelesen (kein Entpacken von ~35 GB nötig).

---

## Inhalt

1. [Voraussetzungen & Installation](#voraussetzungen--installation)
2. [Schnellstart](#schnellstart)
3. [Befehle](#befehle)
4. [Was genau passiert (Regeln)](#was-genau-passiert-regeln)
5. [Geschriebene Metadaten](#geschriebene-metadaten)
6. [Ordner, Logs & Reports](#ordner-logs--reports)
7. [Import in iCloud](#import-in-icloud)
8. [Fehlersuche](#fehlersuche)
9. [Entwicklung](#entwicklung)

---

## Voraussetzungen & Installation

| Was | Wozu | Installation |
|---|---|---|
| Python ≥ 3.11 | Laufzeit | python.org |
| ExifTool | Metadaten schreiben/prüfen | `winget install OliverBetz.ExifTool` |
| ffmpeg | Video-Overlays | kommt automatisch über das pip-Paket `imageio-ffmpeg` |
| NVIDIA-GPU (optional) | schnelles HEVC-Encoding (NVENC) | Treiber; ohne GPU wird automatisch `libx265` (CPU) genutzt |

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

ExifTool wird automatisch gefunden (PATH, `%LOCALAPPDATA%\Programs\ExifTool`, `C:\Program Files\ExifTool`).
Ansonsten Pfad mit `--exiftool C:\pfad\exiftool.exe` oder der Umgebungsvariable `SNAPMEM_EXIFTOOL` angeben.

Alle Befehle werden im Projektordner ausgeführt: `.venv\Scripts\python -m snapmem <befehl>`.
(Alternativ einmal `.venv\Scripts\pip install -e .`, dann gibt es den Befehl `snapmem`.)

## Schnellstart

1. Alle Zip-Dateien des Exports (`mydata~….zip`, `mydata~…-2.zip`, …) nach `input/` legen – **alle** Teile,
   die JSON mit Datum/Ort liegt nur im ersten Zip.
2. Umgebung prüfen:
   ```bash
   .venv\Scripts\python -m snapmem check
   ```
3. Export analysieren (erstellt Katalog + Reports, ca. 1–2 Minuten beim ersten Mal):
   ```bash
   .venv\Scripts\python -m snapmem scan
   ```
4. Konflikte entscheiden (öffnet den Browser):
   ```bash
   .venv\Scripts\python -m snapmem review
   ```
5. Alles verarbeiten und prüfen:
   ```bash
   .venv\Scripts\python -m snapmem run
   ```
6. Ergebnis in `output/` → in iCloud importieren.

Zum Ausprobieren zuerst nur je ein Beispiel pro Szenario in einen Testordner verarbeiten:

```bash
.venv\Scripts\python -m snapmem process --sample --output _test_output
```

Ein Abbruch (Strg+C, Absturz, Neustart) ist unkritisch: Ein erneuter `run` setzt dort fort, wo er aufgehört
hat (Status in `work/manifest.jsonl`). Mit `--force` wird alles neu erzeugt.

## Befehle

| Befehl | Beschreibung |
|---|---|
| `check` | Prüft ffmpeg, ExifTool, NVENC und den Input-Ordner. |
| `scan` | Liest alle Zips + JSON, baut den Katalog (`work/catalog.json`) und schreibt Reports. |
| `review` | Lokale Web-Oberfläche (`http://127.0.0.1:8765`) zum Entscheiden von Konflikten. |
| `process` | Verarbeitet Memories nach `output/`. Optionen: `--sample`, `--only ID…`, `--limit N`, `--dry-run`. |
| `run` | `scan` + `process` + `verify` in einem Schritt. |
| `verify` | Liest die Metadaten aller Ausgabedateien mit ExifTool zurück und vergleicht sie mit dem Katalog. |

Gemeinsame Optionen: `--input`, `--output`, `--work` (Ordner), `--exiftool`, `--default-tz` (Standard
`Europe/Berlin`), `-v` (Debug-Ausgabe). Für `process`/`run` zusätzlich: `--workers` (Standard 6),
`--video-workers` (parallele Video-Encodes, Standard 2), `--use-defaults` (offene Konflikte automatisch lösen),
`--force`.

## Was genau passiert (Regeln)

### Zuordnung Datei ↔ Datum/Ort
Die `memories_history.json` enthält Datum (UTC), Typ und Ort, aber **keinen Dateinamen**. Jeder Eintrag
in den Zips trägt jedoch einen sekundengenauen UTC-Zeitstempel (Zip-Feld „extended timestamp“), der exakt dem
JSON-Datum entspricht. Darüber wird zugeordnet (Zeitpunkt + Typ Bild/Video). JSON-Einträge ohne Datei
werden im Report `json_ohne_datei.csv` aufgelistet. Ort `0.0, 0.0` bedeutet „kein Standort“.

### Overlays
Zu einer `…-main.jpg/.mp4` gehört optional eine `…-overlay.png`. Die Endung täuscht – tatsächlich vorkommende
Formate: PNG, **WebP** und Snapchats eigener Container **SCOF**. SCOF enthält mehrere Ebenen: eine transparente
Text-/Sticker-Ebene und ggf. eine deckende Lens-Ebene (z. B. Filter, Himmel-Effekt), die das Foto ersetzt.
Ergebnis = Lens-Ebene (falls vorhanden) + Text/Sticker darüber – so wie der Snap damals aussah.

Das Overlay wird auf die Größe des Mediums skaliert („A – strecken“). Weicht das Seitenverhältnis um mehr als
6 % ab (z. B. 3:4-Foto mit 9:16-Overlay), entsteht ein **Fit-Konflikt**: im Review wählst du zwischen
A (strecken), B (einpassen), C (füllen) und D (Letterbox: Medium mit schwarzen Rändern auf 9:16-Leinwand,
wie Snapchat es anzeigt). Ohne Entscheidung wird diese Memory nicht verarbeitet (oder mit `--use-defaults` → D).

### Bilder
- ohne Overlay: **bitgenaue Kopie**, nur Metadaten werden ergänzt.
- mit Overlay: zusammengefügt, gespeichert als JPEG (Qualität 95, 4:4:4 für scharfen Text).

### Videos
- ohne Overlay: **verlustfreie Kopie** (Container bleibt; interne QuickTime-Dateien bekommen die Endung `.mov`).
- mit Overlay: Overlay wird eingebrannt, Kodierung **HEVC (H.265) über NVENC** (Fallback `libx265`), mit
  Apple-kompatiblem `hvc1`-Tag; Ton wird unverändert übernommen, Drehung (Hochkant-Videos) korrekt berücksichtigt,
  Dauer wird nach dem Encoding geprüft.

### Duplikate
Snapchat-Exporte enthalten Memories mehrfach (erneutes Speichern → gleicher Inhalt, späteres Datum).
Identischer Inhalt (Hauptdatei **und** Overlay byte-gleich) wird nur einmal ausgegeben – **mit dem ältesten
Datum** (= Original). Liste: `reports/duplikate.csv`.

### Datum
- Grundsätzlich: Snapchat-Datum aus der JSON (UTC).
- Ausnahme Videos: Liegt das im Video eingebettete Aufnahmedatum mehr als 1 Tag **vor** dem Snapchat-Datum, handelt
  es sich um einen Import aus der Kamerarolle → das eingebettete (echte) Datum wird verwendet und der JSON-Standort
  verworfen (er beschreibt nur, wo gespeichert wurde). Liegt es später (Neukodierung durch Snapchat), wird es ignoriert.

### Zeitzone
Die lokale Uhrzeit wird aus dem GPS-Standort bestimmt (Offline-Datenbank `timezonefinder`, inkl. Sommerzeit),
ohne GPS gilt `Europe/Berlin` (`--default-tz`). Beispiel: Kenia → UTC+3, Seoul → UTC+9.

### Standort-Konflikte
Gibt es für denselben Zeitpunkt mehrere JSON-Einträge desselben Typs mit unterschiedlichen Orten, ist unklar,
welcher Ort zu welcher Datei gehört. Im Review siehst du Vorschau/Video, die Kandidaten auf einer Karte und
zusätzlich andere Memories aus ±6 Stunden als Orientierung; du wählst einen Kandidaten, den Mittelpunkt,
klickst einen eigenen Punkt oder „kein Standort“. Mit `--use-defaults` wird kein GPS geschrieben.

### Dateinamen
`output/YYYY-MM-DD_HH-MM-SS_XXXXXXXX.ext` – lokale Aufnahmezeit + die ersten 8 Zeichen der Snapchat-ID.
Änderungs- und Erstelldatum der Datei werden auf den Aufnahmezeitpunkt gesetzt.

## Geschriebene Metadaten

**Fotos (EXIF/XMP)**

| Tag | Inhalt |
|---|---|
| `DateTimeOriginal`, `CreateDate`, `ModifyDate` | lokale Aufnahmezeit |
| `OffsetTimeOriginal`, `OffsetTimeDigitized`, `OffsetTime` | Zeitzonen-Offset, z. B. `+02:00` |
| `XMP-photoshop:DateCreated` | lokale Zeit mit Offset |
| `GPSLatitude/Ref`, `GPSLongitude/Ref`, `GPSDateStamp`, `GPSTimeStamp` | Standort + UTC-Zeit |

**Videos (QuickTime)**

| Tag | Inhalt |
|---|---|
| `CreateDate`, `ModifyDate`, `Track*/Media*Date` | Aufnahmezeit in UTC (QuickTime-Standard) |
| `Keys:CreationDate` (`com.apple.quicktime.creationdate`) | lokale Zeit mit Offset – von Apple Fotos bevorzugt |
| `Keys:GPSCoordinates` (`com.apple.quicktime.location.ISO6709`) | Standort (Apple) |
| `UserData:GPSCoordinates` (`©xyz`) | Standort (Kompatibilität) |

Prüfen lässt sich das jederzeit mit `snapmem verify` oder z. B.
`exiftool -G1 -time:all -gps:all -Keys:all <datei>`.

## Ordner, Logs & Reports

```
input/      Zip-Dateien des Exports (wird nur gelesen)
output/     Ergebnis – genau das kommt in die Fotobibliothek
work/       catalog.json, probe_cache.json, decisions.json, manifest.jsonl, tmp/
reports/    catalog.csv, duplikate.csv, json_ohne_datei.csv, process_*.csv, verify_*.csv
logs/       ein Log pro Lauf (Konsole = INFO, Datei = DEBUG inkl. aller ffmpeg/exiftool-Aufrufe)
```

Reports und Logs liegen bewusst **nicht** in `output/`, damit sie nicht mit importiert werden.
`catalog.csv` (Excel-tauglich, `;`-getrennt) zeigt für jede Memory Quelle, Datum (Snapchat/Video/final),
Zeitzone, Ort, Fit, Ausgabename und Hinweise. Bei Problemen zuerst ins Log von `logs/` schauen – Fehler stehen
dort mit vollständigem Traceback.

## Import in iCloud

- **Mac (Fotos-App)**: Ablage → Importieren → `output/` wählen. Die Fotos werden nach Aufnahmedatum einsortiert
  und mit iCloud synchronisiert.
- **Windows**: In der App „iCloud für Windows“ Fotos aktivieren und die Dateien in den Upload-Ordner kopieren,
  oder auf icloud.com/photos per Upload (dort in Etappen von einigen hundert Dateien hochladen).
- **iPhone**: Dateien z. B. per AirDrop/Finder übertragen und „In Fotos sichern“.

Hinweis: Bereits importierte Dateien erneut zu importieren erzeugt Duplikate in der Bibliothek – am besten
einmal komplett verarbeiten, Stichproben prüfen und dann importieren.

## Fehlersuche

| Symptom | Lösung |
|---|---|
| `exiftool nicht gefunden` | `winget install OliverBetz.ExifTool`, neues Terminal öffnen oder `--exiftool` angeben. |
| `hevc_nvenc` nicht verfügbar | Wird automatisch durch `libx265` ersetzt (langsamer). `check` zeigt den Status. |
| Memories werden übersprungen („offene Konflikte“) | `review` ausführen oder `--use-defaults`. |
| Karte im Review leer | Die Karte lädt Kacheln von openstreetmap.org (Internet nötig). Kandidaten-Buttons funktionieren auch offline. |
| Einzelne Datei erneut erzeugen | `process --only <ID> --force` (ID steht in `reports/catalog.csv`). |
| Komplett neu beginnen | `output/` und `work/manifest.jsonl` löschen (Cache und Entscheidungen bleiben erhalten). |

## Entwicklung

```
snapmem/
  cli.py            Kommandozeile
  config.py         Einstellungen & Pfade
  export_reader.py  Zips, Zeitstempel, JSON, MP4-Header, Format-Erkennung
  catalog.py        Zuordnung, Duplikate, Datum, Zeitzone, Konflikte, Dateinamen
  decisions.py      Entscheidungen aus dem Review
  media/overlay.py  PNG/WebP/SCOF laden
  media/images.py   Bild-Compositing, Fit-Modi
  media/videos.py   ffmpeg-Overlay-Encoding
  metadata.py       ExifTool (Schreiben/Lesen), Dateizeiten
  pipeline.py       parallele Verarbeitung, Fortsetzen, Szenario-Stichprobe
  verify.py         Kontrolle der Ausgabe
  review/           Web-Oberfläche (Server + static/index.html)
tests/              pytest, inkl. End-to-End mit synthetischem Export
```

Tests ausführen:

```bash
.venv\Scripts\python -m pytest
```

Die Tests erzeugen einen kleinen künstlichen Export (inkl. Zip-Zeitstempeln, WebP-/SCOF-Overlays, Duplikat,
Kamerarollen-Video, Standort- und Fit-Konflikt) – es werden keine echten Daten benötigt oder eingecheckt.
