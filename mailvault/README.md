# MailVault – Thunderbird Mail Cleanup Tool

Intelligentes Mail-Analyse- und Aufräum-Tool für Thunderbird (IMAP).
Liest Mails aus Thunderbird's lokalen IMAP-Cache, analysiert sie und ermöglicht Bulk-Cleanup über ein Flask Web-UI.

## Features

- **Mail-Import:** Parst Thunderbird's lokale mbox/maildir-Dateien und indexiert Metadaten in SQLite
- **Absender-Übersicht:** Gruppiert nach Absender mit Mail-Anzahl, letztem Datum, durchschnittlichem Score
- **Relevanz-Score:** Regelbasiertes Scoring (Newsletter-Detection, Keyword-Analyse, Header-Checks)
- **Bulk-Cleanup:** Absender auswählen → alle Mails markieren/löschen
- **IMAP-Sync:** Löschungen werden per IMAP auf dem Server durchgeführt (sicher & sauber)
- **ML-Ready:** Scoring-Architektur ist modular – TF-IDF/ML-Scorer kann später ergänzt werden

## Architektur

```
mailvault/
├── app.py                 # Flask-App, Routen
├── config.py              # Konfiguration (IMAP-Credentials, Pfade)
├── requirements.txt       # Dependencies
├── models.py              # SQLAlchemy Models
├── scanner.py             # Thunderbird mbox/IMAP Scanner
├── scorer.py              # Relevanz-Scoring (regelbasiert + ML-Interface)
├── imap_client.py         # IMAP-Verbindung für Löschungen
├── templates/
│   ├── base.html
│   ├── dashboard.html     # Hauptübersicht
│   ├── sender_detail.html # Mails eines Absenders
│   └── settings.html      # IMAP-Config
├── static/
│   └── style.css
└── mailvault.db           # SQLite (wird erstellt)
```

## Setup

```bash
cd mailvault
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.py.example config.py  # IMAP-Daten eintragen
python app.py
```

Dann im Browser: http://localhost:5000

## Sicherheit

- IMAP-Passwörter werden nur lokal in config.py gespeichert
- Löschungen erfolgen über IMAP (nicht durch Manipulation lokaler Dateien)
- Vor Bulk-Delete wird eine Bestätigung verlangt
