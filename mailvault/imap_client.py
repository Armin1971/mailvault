"""MailVault IMAP Client – Führt Löschungen auf dem IMAP-Server durch."""

import imaplib
from models import db, Mail, ImapAccount


def get_imap_connection(account):
    """Stellt eine IMAP-Verbindung her."""
    if account.use_ssl:
        conn = imaplib.IMAP4_SSL(account.server, account.port)
    else:
        conn = imaplib.IMAP4(account.server, account.port)
    conn.login(account.username, account.password)
    return conn


def delete_mails_by_sender(account_id, sender_email):
    """Löscht alle Mails eines Absenders via IMAP.

    Strategie: Sucht in jedem Ordner nach Mails des Absenders
    und markiert sie als gelöscht.
    """
    account = ImapAccount.query.get(account_id)
    if not account:
        raise ValueError(f"Account {account_id} nicht gefunden")

    conn = get_imap_connection(account)
    deleted_count = 0
    errors = []

    try:
        # Alle Ordner durchgehen
        _, folder_list = conn.list()
        for folder_entry in folder_list:
            if isinstance(folder_entry, bytes):
                # Ordnername extrahieren
                parts = folder_entry.decode("utf-8", errors="replace")
                # Letztes Element nach dem letzten Separator
                folder_name = parts.split('"')[-2] if '"' in parts else parts.split()[-1]

                try:
                    status, _ = conn.select(f'"{folder_name}"')
                    if status != "OK":
                        continue

                    # Nach Absender suchen
                    _, msg_nums = conn.search(None, f'FROM "{sender_email}"')
                    if not msg_nums[0]:
                        continue

                    uids = msg_nums[0].split()
                    for uid in uids:
                        try:
                            # Als gelöscht markieren
                            conn.store(uid, "+FLAGS", "\\Deleted")
                            deleted_count += 1
                        except Exception as e:
                            errors.append(f"Fehler bei UID {uid}: {e}")

                    # Gelöschte Mails endgültig entfernen
                    conn.expunge()

                except Exception as e:
                    errors.append(f"Fehler bei Ordner {folder_name}: {e}")

    finally:
        try:
            conn.logout()
        except Exception:
            pass

    # In lokaler DB als gelöscht markieren
    mails = Mail.query.filter_by(sender_email=sender_email, account_id=account_id).all()
    for mail in mails:
        mail.is_deleted = True
    db.session.commit()

    return {"deleted": deleted_count, "errors": errors}


def delete_mails_by_ids(account_id, mail_ids):
    """Löscht spezifische Mails via IMAP anhand ihrer DB-IDs."""
    account = ImapAccount.query.get(account_id)
    if not account:
        raise ValueError(f"Account {account_id} nicht gefunden")

    mails = Mail.query.filter(Mail.id.in_(mail_ids), Mail.account_id == account_id).all()
    if not mails:
        return {"deleted": 0, "errors": ["Keine Mails gefunden"]}

    conn = get_imap_connection(account)
    deleted_count = 0
    errors = []

    try:
        # Gruppiere nach Ordner
        by_folder = {}
        for mail in mails:
            folder = mail.imap_folder or "INBOX"
            if folder not in by_folder:
                by_folder[folder] = []
            by_folder[folder].append(mail)

        for folder, folder_mails in by_folder.items():
            try:
                status, _ = conn.select(f'"{folder}"')
                if status != "OK":
                    errors.append(f"Ordner {folder} nicht auswählbar")
                    continue

                for mail in folder_mails:
                    if mail.imap_uid:
                        try:
                            conn.store(mail.imap_uid.encode(), "+FLAGS", "\\Deleted")
                            mail.is_deleted = True
                            deleted_count += 1
                        except Exception as e:
                            errors.append(f"Fehler bei {mail.subject}: {e}")
                    elif mail.message_id:
                        # Fallback: Nach Message-ID suchen
                        try:
                            _, msg_nums = conn.search(
                                None, f'HEADER Message-ID "{mail.message_id}"'
                            )
                            if msg_nums[0]:
                                for uid in msg_nums[0].split():
                                    conn.store(uid, "+FLAGS", "\\Deleted")
                                mail.is_deleted = True
                                deleted_count += 1
                        except Exception as e:
                            errors.append(f"Fallback-Fehler: {e}")

                conn.expunge()

            except Exception as e:
                errors.append(f"Ordner-Fehler {folder}: {e}")

    finally:
        try:
            conn.logout()
        except Exception:
            pass

    db.session.commit()
    return {"deleted": deleted_count, "errors": errors}


def test_connection(server, port, use_ssl, username, password):
    """Testet eine IMAP-Verbindung."""
    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(server, port)
        else:
            conn = imaplib.IMAP4(server, port)
        conn.login(username, password)

        # Ordner zählen
        _, folder_list = conn.list()
        folder_count = len(folder_list) if folder_list else 0

        conn.logout()
        return {"success": True, "folders": folder_count}
    except Exception as e:
        return {"success": False, "error": str(e)}
