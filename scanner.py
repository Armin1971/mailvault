"""MailVault Scanner – Liest Mails aus Thunderbird-Cache oder direkt via IMAP."""

import mailbox
import email
import email.header
import email.utils
import os
import glob
import json
import imaplib
import re
from datetime import datetime
from bs4 import BeautifulSoup

from models import db, Mail, Sender, ImapAccount


def decode_header_value(value):
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded).strip()


def extract_email_address(from_header):
    if not from_header:
        return "", ""
    name, addr = email.utils.parseaddr(from_header)
    name = decode_header_value(name) if name else ""
    return addr.lower().strip(), name


def parse_date(date_str):
    if not date_str:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed.replace(tzinfo=None)
    except Exception:
        return None


def extract_body(msg):
    body_text = ""
    body_html = ""
    has_html = False

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disp = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disp:
                continue
            try:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if content_type == "text/plain":
                body_text += text
            elif content_type == "text/html":
                body_html += text
                has_html = True
    else:
        content_type = msg.get_content_type()
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(charset, errors="replace")
                if content_type == "text/html":
                    body_html = text
                    has_html = True
                else:
                    body_text = text
        except Exception:
            pass

    if not body_text and body_html:
        soup = BeautifulSoup(body_html, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        body_text = soup.get_text(separator=" ", strip=True)

    return body_text, has_html


def has_attachments(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get("Content-Disposition") and "attachment" in str(
                part.get("Content-Disposition")
            ):
                return True
    return False


def get_or_create_sender(addr, name):
    sender = Sender.query.filter_by(email=addr).first()
    if not sender:
        sender = Sender(email=addr, display_name=name)
        db.session.add(sender)
        db.session.flush()
    elif name and not sender.display_name:
        sender.display_name = name
    return sender


def process_message(msg, folder_name, account_id=None, mail_size=0):
    message_id = msg.get("Message-ID", "")
    if message_id and Mail.query.filter_by(message_id=message_id).first():
        return None

    from_header = decode_header_value(msg.get("From", ""))
    addr, name = extract_email_address(from_header)
    if not addr:
        return None

    subject = decode_header_value(msg.get("Subject", "(kein Betreff)"))
    date = parse_date(msg.get("Date"))
    body_text, has_html = extract_body(msg)
    has_attach = has_attachments(msg)
    has_unsub = bool(msg.get("List-Unsubscribe"))
    has_list = bool(msg.get("List-Id"))
    is_noreply = bool(
        re.search(r"no[-_]?reply|noreply|donotreply", addr, re.IGNORECASE)
    )

    sender = get_or_create_sender(addr, name)

    mail = Mail(
        message_id=message_id,
        account_id=account_id,
        sender_id=sender.id,
        sender_email=addr,
        sender_name=name,
        subject=subject,
        date=date,
        folder=folder_name,
        body_preview=body_text[:500] if body_text else "",
        body_length=len(body_text),
        mail_size=mail_size or len(body_text),
        has_html=has_html,
        has_attachments=has_attach,
        has_unsubscribe=has_unsub,
        has_list_header=has_list,
        is_noreply=is_noreply,
    )
    db.session.add(mail)
    return mail


def scan_thunderbird_profile(profile_path=None, on_progress=None):
    from config import THUNDERBIRD_PROFILE
    base = profile_path or THUNDERBIRD_PROFILE
    stats = {"scanned": 0, "imported": 0, "errors": 0, "folders": []}

    profiles = glob.glob(os.path.join(base, "*.default*"))
    if not profiles:
        profiles = [base]

    for profile in profiles:
        imap_dirs = glob.glob(os.path.join(profile, "ImapMail", "*"))
        local_dirs = [os.path.join(profile, "Mail", "Local Folders")]

        for mail_dir in imap_dirs + local_dirs:
            if not os.path.isdir(mail_dir):
                continue
            for f in os.listdir(mail_dir):
                filepath = os.path.join(mail_dir, f)
                if os.path.isfile(filepath) and "." not in f:
                    try:
                        mbox = mailbox.mbox(filepath)
                        folder_name = f
                        count = 0
                        for msg in mbox:
                            stats["scanned"] += 1
                            try:
                                result = process_message(msg, folder_name)
                                if result:
                                    stats["imported"] += 1
                                    count += 1
                            except Exception:
                                stats["errors"] += 1
                            if stats["scanned"] % 100 == 0:
                                db.session.commit()
                                if on_progress:
                                    on_progress(
                                        stats["scanned"], 0,
                                        f"Ordner: {folder_name}",
                                        f'{stats["imported"]} importiert',
                                    )
                        if count > 0:
                            stats["folders"].append({"name": folder_name, "path": filepath, "count": count})
                        mbox.close()
                    except Exception:
                        stats["errors"] += 1

    db.session.commit()
    _update_sender_stats()
    return stats


def scan_imap_account(account_id, folders=None, limit=None, since=None, on_progress=None):
    account = ImapAccount.query.get(account_id)
    if not account:
        raise ValueError(f"Account {account_id} nicht gefunden")

    stats = {"scanned": 0, "imported": 0, "errors": 0, "skipped": 0, "folders": []}

    if account.use_ssl:
        conn = imaplib.IMAP4_SSL(account.server, account.port)
    else:
        conn = imaplib.IMAP4(account.server, account.port)

    conn.login(account.username, account.password)

    # Ordner auflisten
    if not folders:
        _, folder_list = conn.list()
        folders = []
        for f in folder_list:
            if isinstance(f, bytes):
                match = re.search(rb'"([^"]*)"$|(\S+)$', f)
                if match:
                    fname = (match.group(1) or match.group(2)).decode("utf-8", errors="replace")
                    folders.append(fname)

    if on_progress:
        on_progress(0, 0, f"{len(folders)} Ordner gefunden", "Zaehle Mails...")

    # Erst zaehlen
    search_criteria = f'SINCE "01-Jan-{since}"' if since else "ALL"
    total_mails = 0
    folder_uids = {}

    for folder in folders:
        try:
            status, _ = conn.select(f'"{folder}"', readonly=True)
            if status != "OK":
                continue
            _, msg_nums = conn.search(None, search_criteria)
            if msg_nums[0]:
                uids = msg_nums[0].split()
                if limit:
                    uids = uids[-limit:]
                folder_uids[folder] = uids
                total_mails += len(uids)
        except Exception:
            pass

    if on_progress:
        on_progress(0, total_mails, f"{total_mails} Mails in {len(folder_uids)} Ordnern", "Starte Download...")

    # Mails herunterladen
    for folder, uids in folder_uids.items():
        try:
            status, _ = conn.select(f'"{folder}"', readonly=True)
            if status != "OK":
                continue

            count = 0
            for i, uid in enumerate(uids):
                stats["scanned"] += 1
                try:
                    _, data = conn.fetch(uid, "(RFC822 RFC822.SIZE)")
                    if not data or not data[0]:
                        continue
                    # RFC822.SIZE aus der Antwort parsen
                    raw_size = 0
                    raw_msg = None
                    for part in data:
                        if isinstance(part, tuple):
                            header_line = part[0].decode() if isinstance(part[0], bytes) else str(part[0])
                            if b'RFC822.SIZE' in part[0] if isinstance(part[0], bytes) else 'RFC822.SIZE' in header_line:
                                import re as _re
                                size_match = _re.search(r'RFC822\.SIZE\s+(\d+)', header_line)
                                if size_match:
                                    raw_size = int(size_match.group(1))
                            if len(part) > 1 and isinstance(part[1], bytes) and len(part[1]) > 100:
                                raw_msg = part[1]
                    if not raw_msg:
                        # Fallback: altes Format
                        raw_msg = data[0][1]
                    msg = email.message_from_bytes(raw_msg)
                    if not raw_size:
                        raw_size = len(raw_msg)
                    result = process_message(msg, folder, account.id, mail_size=raw_size)
                    if result:
                        result.imap_uid = uid.decode() if isinstance(uid, bytes) else str(uid)
                        result.imap_folder = folder
                        stats["imported"] += 1
                        count += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    stats["errors"] += 1

                if stats["scanned"] % 10 == 0:
                    db.session.commit()
                    if on_progress:
                        pct = round(stats["scanned"] / total_mails * 100) if total_mails else 0
                        on_progress(
                            stats["scanned"], total_mails,
                            f"Ordner: {folder} - {pct}%",
                            f'{stats["imported"]} neu, {stats["skipped"]} uebersprungen, {stats["errors"]} Fehler',
                        )

            db.session.commit()
            if count > 0:
                stats["folders"].append({"name": folder, "count": count})
        except Exception:
            stats["errors"] += 1

    conn.logout()
    account.last_scan = datetime.utcnow()
    db.session.commit()
    _update_sender_stats()
    return stats


def _update_sender_stats():
    senders = Sender.query.all()
    for sender in senders:
        mails = Mail.query.filter_by(sender_id=sender.id, is_deleted=False).all()
        sender.mail_count = len(mails)
        if mails:
            dates = [m.date for m in mails if m.date]
            if dates:
                sender.first_seen = min(dates)
                sender.last_seen = max(dates)
            scores = [m.score for m in mails if m.score is not None]
            if scores:
                sender.avg_score = sum(scores) / len(scores)
    db.session.commit()
