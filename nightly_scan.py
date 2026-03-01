#!/usr/bin/env python3
"""MailVault Nightly Scan – Fuer Cron-Job.
Laedt neue Mails, bewertet sie, entfernt Zombies und aktualisiert Statistiken.
Aufruf: python3 /opt/mailvault/nightly_scan.py
Crontab-Eintrag (taeglich um 3:00 Uhr):
  0 3 * * * cd /opt/mailvault && /opt/mailvault/venv/bin/python3 nightly_scan.py >> /opt/mailvault/nightly.log 2>&1
"""
import sys
import os
import time
import imaplib

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from app import app
from models import db, Mail, ImapAccount
from scanner import scan_imap_account, _update_sender_stats
from scorer import score_all_mails


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def cleanup_zombies():
    """Markiert Mails als geloescht die in Gmail nicht mehr existieren."""
    log("Zombie-Cleanup: Pruefe ob DB-Mails noch in Gmail existieren...")

    acc = ImapAccount.query.first()
    if not acc:
        return 0

    try:
        conn = imaplib.IMAP4_SSL(acc.server, acc.port)
        conn.login(acc.username, acc.password)
        conn.select('"[Gmail]/Alle Nachrichten"', readonly=True)

        _, data = conn.uid('SEARCH', None, 'ALL')
        uids = data[0].split() if data[0] else []

        gmail_msgids = set()
        batch_size = 100
        for start in range(0, len(uids), batch_size):
            batch = uids[start:start + batch_size]
            uid_str = b','.join(batch).decode()
            _, fdata = conn.uid('FETCH', uid_str, '(BODY[HEADER.FIELDS (MESSAGE-ID)])')
            for item in fdata:
                if isinstance(item, tuple) and len(item) > 1:
                    line = item[1].decode('utf-8', errors='replace').strip()
                    if line.lower().startswith('message-id:'):
                        msgid = line[11:].strip()
                        gmail_msgids.add(msgid)

        conn.logout()
        log(f"  Gmail: {len(gmail_msgids)} Message-IDs gefunden")

        active_mails = Mail.query.filter_by(is_deleted=False).all()
        zombies = 0
        for m in active_mails:
            if m.message_id and m.message_id not in gmail_msgids:
                m.is_deleted = True
                zombies += 1

        db.session.commit()
        log(f"  {zombies} Zombie-Mails als geloescht markiert")
        return zombies

    except Exception as e:
        log(f"  Zombie-Cleanup Fehler: {e}")
        return 0


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
                stats = scan_imap_account(
                    acc.id,
                    folders=['INBOX', '[Gmail]/Alle Nachrichten'],
                    on_progress=on_progress,
                )
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

        # Zombie-Cleanup: Mails die in Gmail geloescht wurden
        cleanup_zombies()

        log("Aktualisiere Sender-Statistiken...")
        _update_sender_stats()

        elapsed = time.time() - start
        log(f"=== Nightly Scan beendet in {elapsed:.0f}s - {total_imported} neue Mails ===")


if __name__ == "__main__":
    run_nightly()
