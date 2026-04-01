# Oatly-Alert per GitHub Actions

Dieses Projekt prueft regelmaessig die Seite `https://www.aktionspreis.de/angebote/oatly-barista-1l` und sendet eine Telegram-Nachricht, wenn ein aktuelles Oatly-Barista-Angebot erkannt wird oder sich relevante Angebotsdaten geaendert haben.

Beruecksichtigt werden:

- ob aktuell ein Angebot vorhanden ist
- wie viele Angebote gefunden wurden
- welcher Tiefstpreis genannt wird
- welche Haendler genannt werden
- ob sich diese Werte gegenueber dem letzten Lauf geaendert haben

Der letzte erkannte Zustand wird in `state.json` gespeichert. Diese Datei wird lokal ignoriert und in GitHub Actions ueber den Cache zwischen den Workflow-Laeufen erhalten.

## Projektstruktur

- `.github/workflows/oatly-alert.yml`: GitHub-Action fuer geplante und manuelle Ausfuehrung
- `src/check_oatly.py`: Python-Skript fuer Abruf, Analyse, Zustandsvergleich und Telegram-Versand
- `requirements.txt`: benoetigte Python-Abhaengigkeiten
- `.gitignore`: ignorierte lokale Dateien
- `README.md`: diese Dokumentation

## Voraussetzungen

Du brauchst:

- ein GitHub-Repository mit aktiviertem Actions-Tab
- einen Telegram-Bot
- die Chat-ID des Ziel-Chats

## Benoetigte GitHub-Secrets

Diese Secrets muessen im Repository gesetzt werden:

- `TELEGRAM_BOT_TOKEN`: Token deines Telegram-Bots
- `TELEGRAM_CHAT_ID`: Chat-ID, an die die Nachricht gesendet werden soll

Optional kannst du zusaetzlich eine Repository-Variable setzen:

- `PRICE_THRESHOLD`: Preisgrenze, zum Beispiel `1.79`

Wenn `PRICE_THRESHOLD` gesetzt ist, wird nur benachrichtigt, wenn der erkannte Tiefstpreis kleiner oder gleich diesem Wert ist. Ohne gesetzten Grenzwert wird bei jeder relevanten Aenderung benachrichtigt.

## Manuelle Ausfuehrung

So startest du den Workflow manuell:

1. Oeffne dein GitHub-Repository.
2. Gehe auf den Tab `Actions`.
3. Waehle den Workflow `Oatly Alert`.
4. Klicke auf `Run workflow`.

Zusaetzlich laeuft der Workflow automatisch alle 6 Stunden.

## Wie die Erkennung funktioniert

Das Skript:

1. ruft die Aktionspreis-Seite per `requests` ab
2. extrahiert den Seitentext mit `BeautifulSoup`
3. sucht darin nach Angebotsanzahl, Tiefstpreis und Haendlern
4. vergleicht das Ergebnis mit der vorherigen `state.json`
5. sendet nur bei relevanten Aenderungen eine Telegram-Nachricht

Eine Benachrichtigung wird verschickt, wenn:

- erstmals ein Angebot erkannt wurde
- spaeter wieder ein Angebot erkannt wurde
- sich der Tiefstpreis geaendert hat
- sich die Haendler geaendert haben
- sich die Anzahl der Angebote geaendert hat

## Lokale Nutzung

Optional kannst du das Skript auch lokal testen:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PRICE_THRESHOLD="1.79"  # optional, nur falls gewuenscht
python src/check_oatly.py
```

Dabei muessen `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` bereits in deiner Shell gesetzt sein.

Nach dem Lauf liegt eine lokale `state.json` im Projektverzeichnis.

## Typische Fehlerquellen

- `TELEGRAM_BOT_TOKEN` oder `TELEGRAM_CHAT_ID` fehlen: Das Skript bricht mit Exit-Code 1 ab.
- Die Zielseite ist voruebergehend nicht erreichbar: Der Lauf endet mit einer klaren Fehlermeldung.
- Die Seitenstruktur oder die Formulierungen auf Aktionspreis haben sich geaendert: Dann muss die Parsing-Logik in `src/check_oatly.py` angepasst werden.
- `PRICE_THRESHOLD` hat ein ungeeignetes Format: Verwende einen Wert wie `1.79` oder `1,79`.
- Der erste Workflow-Lauf hat noch keinen Cache: Das ist normal. `state.json` wird nach dem ersten erfolgreichen Lauf gespeichert.

## Hinweise zum Cache

GitHub Actions speichert `state.json` nicht direkt im Repository, sondern im Actions-Cache. Dadurch bleibt der zuletzt erkannte Zustand zwischen den Workflow-Laeufen erhalten, ohne dass automatisch Commits erzeugt werden.
