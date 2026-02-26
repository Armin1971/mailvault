"""MailVault IMAP Client – Gmail-kompatible Loeschungen via UID MOVE."""

import imaplib
from models import db, Mail, ImapAccount

TRASH_FOLDERS = ["[Gmail]/Papierkorb", "[Gmail]/Trash", "[Gmail]/Bin"]
ALL_MAIL_FOLDERS = ["[Gmail]/Alle Nachrichten", "[Gmail]/All Mail"]

# Ordner in denen wir suchen (Reihenfolge: erst INBOX, dann Alle Nachrichten)
SEARCH_FOLDERS = ["INBOX"] + ALL_MAIL_FOLDERS


def get_imap_connection(account):
    if account.use_ssl:
        conn = imaplib.IMAP4_SSL(account.server, account.port)
    else:
        conn = imaplib.IMAP4(account.server, account.port)
    conn.login(account.username, account.password)
    return conn


def _find_trash_folder(conn):
    _, folder_list = conn.list()
    available = []
    for entry in folder_list:
        if isinstance(entry, bytes):
            parts = entry.decode("utf-8", errors="replace")
            name = parts.split('"')[-2] if '"' in parts else parts.split()[-1]
            available.append(name)

    for trash in TRASH_FOLDERS:
        if trash in available:
            return trash

    for entry in folder_list:
        if isinstance(entry, bytes):
            decoded = entry.decode("utf-8", errors="replace")
            if "\\Trash" in decoded:
                name = decoded.split('"')[-2] if '"' in decoded else decoded.split()[-1]
                return name
    return None


def _find_available_folders(conn, candidates):
    """Prueft welche Ordner aus der Kandidatenliste existieren."""
    _, folder_list = conn.list()
    available = []
    for entry in folder_list:
        if isinstance(entry, bytes):
            parts = entry.decode("utf-8", errors="replace")
            name = parts.split('"')[-2] if '"' in parts else parts.split()[-1]
            available.append(name)

    result = []
    for candidate in candidates:
        if candidate in available:
            result.append(candidate)
    return result


def delete_mails_by_sender(account_id, sender_email, on_progress=None):
    account = ImapAccount.query.get(account_id)
    if not account:
        raise ValueError(f"Account {account_id} nicht gefunden")

    conn = get_imap_connection(account)
    deleted_count = 0
    errors = []

    try:
        trash_folder = _find_trash_folder(conn)
        folders = _find_available_folders(conn, SEARCH_FOLDERS)

        if not folders:
            folders = ["INBOX"]

        total_found = 0
        folder_uids = {}

        # Erst zaehlen in allen Ordnern
        for folder in folders:
            try:
                status, _ = conn.select(f'"{folder}"')
                if status != "OK":
                    continue
                _, nums = conn.uid("SEARCH", None, f'FROM "{sender_email}"')
                if nums[0]:
                    uids = nums[0].split()
                    folder_uids[folder] = uids
                    total_found += len(uids)
            except Exception:
                pass

        if total_found == 0:
            if on_progress:
                on_progress(1, 1, "Keine Mails gefunden", "0 geloescht")
            mails = Mail.query.filter_by(sender_email=sender_email, account_id=account_id).all()
            for mail in mails:
                mail.is_deleted = True
            db.session.commit()
            conn.logout()
            return {"deleted": 0, "errors": []}

        if on_progress:
            on_progress(0, total_found, f"{total_found} Mails in {len(folder_uids)} Ordnern", "Starte Loeschung...")

        # Loeschen in jedem Ordner
        for folder, uids in folder_uids.items():
            try:
                status, _ = conn.select(f'"{folder}"')
                if status != "OK":
                    continue

                batch_size = 50
                for batch_start in range(0, len(uids), batch_size):
                    batch = uids[batch_start:batch_start + batch_size]
                    uid_str = b",".join(batch).decode()

                    try:
                        if trash_folder:
                            conn.uid("MOVE", uid_str, f'"{trash_folder}"')
                        else:
                            conn.uid("STORE", uid_str, "+FLAGS", "\\Deleted")
                            conn.expunge()
                        deleted_count += len(batch)
                    except Exception:
                        for uid in batch:
                            try:
                                uid_s = uid.decode() if isinstance(uid, bytes) else uid
                                if trash_folder:
                                    conn.uid("MOVE", uid_s, f'"{trash_folder}"')
                                else:
                                    conn.uid("STORE", uid_s, "+FLAGS", "\\Deleted")
                                deleted_count += 1
                            except Exception as e:
                                errors.append(f"UID {uid}: {e}")
                        if not trash_folder:
                            conn.expunge()

                    if on_progress:
                        on_progress(deleted_count, total_found,
                                    f"{folder}: {deleted_count}/{total_found}",
                                    f"{deleted_count} von {total_found} geloescht")

            except Exception as e:
                errors.append(f"Ordner {folder}: {e}")

        if on_progress:
            on_progress(total_found, total_found, "Abgeschlossen", f"{deleted_count} Mails geloescht")

    except Exception as e:
        errors.append(str(e))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    # Lokal markieren
    mails = Mail.query.filter_by(sender_email=sender_email, account_id=account_id).all()
    for mail in mails:
        mail.is_deleted = True
    db.session.commit()

    return {"deleted": deleted_count, "errors": errors}


def delete_mails_by_ids(account_id, mail_ids, on_progress=None):
    account = ImapAccount.query.get(account_id)
    if not account:
        raise ValueError(f"Account {account_id} nicht gefunden")

    mails = Mail.query.filter(Mail.id.in_(mail_ids), Mail.account_id == account_id).all()
    if not mails:
        return {"deleted": 0, "errors": ["Keine Mails gefunden"]}

    conn = get_imap_connection(account)
    deleted_count = 0
    errors = []
    total = len(mails)

    try:
        trash_folder = _find_trash_folder(conn)

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
                    errors.append(f"Ordner {folder} nicht auswaehlbar")
                    continue

                mails_with_uid = [(m, m.imap_uid) for m in folder_mails if m.imap_uid]
                mails_without_uid = [m for m in folder_mails if not m.imap_uid]

                batch_size = 50
                for batch_start in range(0, len(mails_with_uid), batch_size):
                    batch = mails_with_uid[batch_start:batch_start + batch_size]
                    uid_str = ",".join(uid for _, uid in batch)

                    try:
                        if trash_folder:
                            conn.uid("MOVE", uid_str, f'"{trash_folder}"')
                        else:
                            conn.uid("STORE", uid_str, "+FLAGS", "\\Deleted")
                        for m, _ in batch:
                            m.is_deleted = True
                            deleted_count += 1
                    except Exception:
                        for m, uid in batch:
                            try:
                                if trash_folder:
                                    conn.uid("MOVE", uid, f'"{trash_folder}"')
                                else:
                                    conn.uid("STORE", uid, "+FLAGS", "\\Deleted")
                                m.is_deleted = True
                                deleted_count += 1
                            except Exception as e:
                                errors.append(f"{m.subject}: {e}")

                    if on_progress:
                        on_progress(deleted_count, total,
                                    f"Ordner: {folder}",
                                    f"{deleted_count} von {total} geloescht")

                if not trash_folder:
                    conn.expunge()

                for mail in mails_without_uid:
                    try:
                        _, msg_nums = conn.uid("SEARCH", None, f'HEADER Message-ID "{mail.message_id}"')
                        if msg_nums[0]:
                            uid = msg_nums[0].split()[0].decode()
                            if trash_folder:
                                conn.uid("MOVE", uid, f'"{trash_folder}"')
                            else:
                                conn.uid("STORE", uid, "+FLAGS", "\\Deleted")
                            mail.is_deleted = True
                            deleted_count += 1
                    except Exception as e:
                        errors.append(f"Fallback: {e}")

                if not trash_folder:
                    conn.expunge()

            except Exception as e:
                errors.append(f"Ordner {folder}: {e}")

    except Exception as e:
        errors.append(str(e))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    db.session.commit()
    return {"deleted": deleted_count, "errors": errors}


def test_connection(server, port, use_ssl, username, password):
    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(server, port)
        else:
            conn = imaplib.IMAP4(server, port)
        conn.login(username, password)
        _, folder_list = conn.list()
        folder_count = len(folder_list) if folder_list else 0
        conn.logout()
        return {"success": True, "folders": folder_count}
    except Exception as e:
        return {"success": False, "error": str(e)}
