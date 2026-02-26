#!/usr/bin/env python3
"""MailVault Nightly Scan – Fuer Cron-Job.

Laedt neue Mails, bewertet sie und aktualisiert Statistiken.
Aufruf: python3 /opt/mailvault/nightly_scan.py

Crontab-Eintrag (taeglich um 3:00 Uhr):
  0 3 * * * cd /opt/mailvault && /opt/mailvault/venv/bin/python3 nightly_scan.py >> /opt/mailvault/nightly.log 2>&1
"""

import sys
import os
import time

# Sicherstellen dass wir im richtigen Verzeichnis sind
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from app import app
from models import db, ImapAccount
from scanner import scan_imap_account, _update_sender_stats
from scorer import score_all_mails


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_nightly():
    log("=== Nightly Scan gestartet ===")
    start = time.time()

    with app.app_context():
        accounts = ImapAccount.query.all()
        if not accounts:
            log("Keine IMAP-Accounts konfiguriert. Abbruch.")
            return

        total_imported = 0
        for acc in accounts:
            log(f"Scanne Account: {acc.name} ({acc.username})")

            def on_progress(current, total, msg, detail):
                if current > 0 and current % 100 == 0:
                    log(f"  {msg} - {detail}")

            try:
                stats = scan_imap_account(acc.id, on_progress=on_progress)
                imported = stats.get("imported", 0)
                skipped = stats.get("skipped", 0)
                errors = stats.get("errors", 0)
                total_imported += imported
                log(f"  Fertig: {imported} neu, {skipped} uebersprungen, {errors} Fehler")
            except Exception as e:
                log(f"  FEHLER: {e}")

        if total_imported > 0:
            log(f"Bewerte {total_imported} neue Mails...")
            scored = score_all_mails()
            log(f"  {scored} Mails bewertet")

        log("Aktualisiere Sender-Statistiken...")
        _update_sender_stats()

        elapsed = time.time() - start
        log(f"=== Nightly Scan beendet in {elapsed:.0f}s - {total_imported} neue Mails ===")


if __name__ == "__main__":
    run_nightly()
