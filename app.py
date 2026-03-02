"""MailVault – Flask Application mit Background-Tasks und Live-Progress."""

import json
import uuid
import threading
import time
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, Response, stream_with_context,
)
from models import db, Mail, Sender, ImapAccount
from scanner import scan_thunderbird_profile, scan_imap_account, _update_sender_stats
from scorer import score_all_mails
from imap_client import delete_mails_by_sender, delete_mails_by_ids, test_connection
from tasks import task_manager
import config

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

db.init_app(app)

with app.app_context():
    db.create_all()


# ─── Dashboard ───────────────────────────────────────────────────────────────


@app.route("/")
def dashboard():
    sort = request.args.get("sort", "count")
    order = request.args.get("order", "desc")
    category = request.args.get("category", "all")

    query = Sender.query.filter(Sender.mail_count > 0)

    if category and category != "all":
        query = query.filter_by(category=category)

    sort_col = {
        "count": Sender.mail_count,
        "score": Sender.avg_score,
        "name": Sender.display_name,
        "email": Sender.email,
        "last": Sender.last_seen,
    }.get(sort, Sender.mail_count)

    if sort == "score_count":
        query = query.order_by(Sender.avg_score.asc(), Sender.mail_count.desc())
    elif order == "asc":
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    senders = query.all()

    total_mails = Mail.query.filter_by(is_deleted=False).count()
    total_senders = Sender.query.filter(Sender.mail_count > 0).count()
    avg_score = db.session.query(db.func.avg(Mail.score)).filter(
        Mail.is_deleted == False
    ).scalar() or 0
    low_score_count = Mail.query.filter(Mail.score < 30, Mail.is_deleted == False).count()

    stats = {
        "total_mails": total_mails,
        "total_senders": total_senders,
        "avg_score": round(avg_score, 1),
        "low_score_count": low_score_count,
    }

    categories = (
        db.session.query(Sender.category, db.func.count(Sender.id))
        .filter(Sender.mail_count > 0)
        .group_by(Sender.category)
        .all()
    )

    return render_template(
        "dashboard.html",
        senders=senders,
        stats=stats,
        categories=dict(categories),
        current_sort=sort,
        current_order=order,
        current_category=category,
    )


# ─── Absender-Detail ─────────────────────────────────────────────────────────


@app.route("/sender/<int:sender_id>")
def sender_detail(sender_id):
    sender = Sender.query.get_or_404(sender_id)
    sort = request.args.get("sort", "date")
    order = request.args.get("order", "desc")

    query = Mail.query.filter_by(sender_id=sender.id, is_deleted=False)

    sort_col = {
        "date": Mail.date,
        "score": Mail.score,
        "subject": Mail.subject,
    }.get(sort, Mail.date)

    if order == "asc":
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    mails = query.all()

    return render_template(
        "sender_detail.html",
        sender=sender,
        mails=mails,
        current_sort=sort,
        current_order=order,
    )


# ─── Loeschen (async) ────────────────────────────────────────────────────────


@app.route("/sender/<int:sender_id>/delete-all", methods=["POST"])
def delete_sender_mails(sender_id):
    sender = Sender.query.get_or_404(sender_id)
    task_id = str(uuid.uuid4())[:8]
    task_manager.create_task(task_id, f"Loesche Mails von {sender.email}")

    def run_delete():
        with app.app_context():
            try:
                accounts = ImapAccount.query.all()
                total_deleted = 0
                if accounts:
                    for account in accounts:
                        def on_prog(current, total, msg, detail):
                            task_manager.update(task_id, current, total, msg, detail)
                        result = delete_mails_by_sender(account.id, sender.email, on_prog)
                        total_deleted += result["deleted"]
                else:
                    Mail.query.filter_by(sender_email=sender.email).update(
                        {"is_deleted": True}, synchronize_session=False
                    )
                    db.session.commit()

                s = Sender.query.get(sender_id)
                if s:
                    s.is_blocked = True
                    s.mail_count = 0
                    db.session.commit()

                _update_sender_stats()
                task_manager.finish(task_id, {"deleted": total_deleted})
            except Exception as e:
                task_manager.fail(task_id, str(e))

    threading.Thread(target=run_delete, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/mails/delete", methods=["POST"])
def delete_selected_mails():
    if request.is_json:
        data = request.get_json()
        mail_ids = data.get("mail_ids", [])
    else:
        mail_ids = request.form.getlist("mail_ids", type=int)

    if not mail_ids:
        if request.is_json:
            return jsonify({"error": "Keine Mails ausgewaehlt"}), 400
        flash("Keine Mails ausgewaehlt", "warning")
        return redirect(request.referrer or url_for("dashboard"))

    task_id = str(uuid.uuid4())[:8]
    task_manager.create_task(task_id, f"{len(mail_ids)} Mails loeschen")

    def run_delete():
        with app.app_context():
            try:
                accounts = ImapAccount.query.all()
                if accounts:
                    for account in accounts:
                        def on_prog(current, total, msg, detail):
                            task_manager.update(task_id, current, total, msg, detail)
                        delete_mails_by_ids(account.id, mail_ids, on_prog)
                else:
                    Mail.query.filter(Mail.id.in_(mail_ids)).update(
                        {"is_deleted": True}, synchronize_session=False
                    )
                    db.session.commit()
                _update_sender_stats()
                task_manager.finish(task_id, {"deleted": len(mail_ids)})
            except Exception as e:
                task_manager.fail(task_id, str(e))

    threading.Thread(target=run_delete, daemon=True).start()

    if request.is_json:
        return jsonify({"task_id": task_id})
    flash(f"Loeschung von {len(mail_ids)} Mails gestartet...", "info")
    return redirect(request.referrer or url_for("dashboard"))


# ─── Scan (async) ────────────────────────────────────────────────────────────


@app.route("/scan", methods=["GET", "POST"])
def scan():
    if request.method == "POST":
        scan_type = request.form.get("scan_type", "thunderbird")
        task_id = str(uuid.uuid4())[:8]
        task_manager.create_task(task_id, f"{scan_type.upper()}-Scan")

        if scan_type == "thunderbird":
            def run_scan():
                with app.app_context():
                    try:
                        def on_prog(current, total, msg, detail):
                            task_manager.update(task_id, current, total, msg, detail)
                        stats = scan_thunderbird_profile(on_progress=on_prog)
                        task_manager.update(task_id, message="Berechne Scores...")
                        scored = score_all_mails()
                        task_manager.finish(task_id, {
                            "imported": stats["imported"],
                            "scored": scored,
                            "errors": stats["errors"],
                        })
                    except Exception as e:
                        task_manager.fail(task_id, str(e))

            threading.Thread(target=run_scan, daemon=True).start()

        elif scan_type == "imap":
            account_id = request.form.get("account_id", type=int)
            limit = request.form.get("limit", type=int) or None
            since = request.form.get("since", type=int, default=2025)

            def run_scan():
                with app.app_context():
                    try:
                        def on_prog(current, total, msg, detail):
                            task_manager.update(task_id, current, total, msg, detail)
                        stats = scan_imap_account(
                            account_id, limit=limit, since=since, on_progress=on_prog,
                        )
                        task_manager.update(task_id, message="Berechne Scores...")
                        scored = score_all_mails()
                        task_manager.finish(task_id, {
                            "imported": stats["imported"],
                            "skipped": stats.get("skipped", 0),
                            "scored": scored,
                            "errors": stats["errors"],
                        })
                    except Exception as e:
                        task_manager.fail(task_id, str(e))

            threading.Thread(target=run_scan, daemon=True).start()

        return jsonify({"task_id": task_id})

    accounts = ImapAccount.query.all()
    return render_template("scan.html", accounts=accounts)


# ─── Progress SSE ────────────────────────────────────────────────────────────


@app.route("/api/task/<task_id>/stream")
def task_stream(task_id):
    def generate():
        while True:
            task = task_manager.get(task_id)
            if not task:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                break
            yield f"data: {json.dumps(task)}\n\n"
            if task["status"] in ("done", "error"):
                break
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/task/<task_id>")
def task_status(task_id):
    task = task_manager.get(task_id)
    if not task:
        return jsonify({"error": "Task nicht gefunden"}), 404
    return jsonify(task)


# ─── Einstellungen ───────────────────────────────────────────────────────────


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_account":
            account = ImapAccount(
                name=request.form["name"],
                server=request.form["server"],
                port=int(request.form.get("port", 993)),
                use_ssl="use_ssl" in request.form,
                username=request.form["username"],
                password=request.form["password"],
            )
            db.session.add(account)
            db.session.commit()
            flash(f'Account "{account.name}" hinzugefuegt', "success")

        elif action == "test_account":
            result = test_connection(
                request.form["server"],
                int(request.form.get("port", 993)),
                "use_ssl" in request.form,
                request.form["username"],
                request.form["password"],
            )
            if result["success"]:
                flash(f'Verbindung erfolgreich! {result["folders"]} Ordner gefunden.', "success")
            else:
                flash(f'Verbindung fehlgeschlagen: {result["error"]}', "error")

        elif action == "delete_account":
            account_id = request.form.get("account_id", type=int)
            account = ImapAccount.query.get(account_id)
            if account:
                db.session.delete(account)
                db.session.commit()
                flash(f'Account "{account.name}" geloescht', "success")

        return redirect(url_for("settings"))

    accounts = ImapAccount.query.all()
    return render_template("settings.html", accounts=accounts)


# ─── API ─────────────────────────────────────────────────────────────────────


@app.route("/api/sender/<int:sender_id>/block", methods=["POST"])
def api_block_sender(sender_id):
    sender = Sender.query.get_or_404(sender_id)
    sender.is_blocked = not sender.is_blocked
    db.session.commit()
    return jsonify({"blocked": sender.is_blocked})


@app.route("/api/stats")
def api_stats():
    return jsonify({
        "total_mails": Mail.query.filter_by(is_deleted=False).count(),
        "total_senders": Sender.query.filter(Sender.mail_count > 0).count(),
    })


# ─── Bulk-Aktionen ───────────────────────────────────────────────────────────


@app.route("/bulk/delete-low-score", methods=["POST"])
def bulk_delete_low_score():
    threshold = request.form.get("threshold", type=int, default=20)
    mails = Mail.query.filter(Mail.score < threshold, Mail.is_deleted == False).all()

    if not mails:
        flash("Keine Mails unter diesem Score gefunden", "info")
        return redirect(url_for("dashboard"))

    mail_ids = [m.id for m in mails]
    task_id = str(uuid.uuid4())[:8]
    task_manager.create_task(task_id, f"Bulk-Delete: {len(mail_ids)} Mails")

    def run_delete():
        with app.app_context():
            try:
                accounts = ImapAccount.query.all()
                if accounts:
                    for account in accounts:
                        def on_prog(current, total, msg, detail):
                            task_manager.update(task_id, current, total, msg, detail)
                        delete_mails_by_ids(account.id, mail_ids, on_prog)
                else:
                    Mail.query.filter(Mail.id.in_(mail_ids)).update(
                        {"is_deleted": True}, synchronize_session=False
                    )
                    db.session.commit()
                _update_sender_stats()
                task_manager.finish(task_id, {"deleted": len(mail_ids)})
            except Exception as e:
                task_manager.fail(task_id, str(e))

    threading.Thread(target=run_delete, daemon=True).start()
    flash(f"Loeschung von {len(mail_ids)} Mails gestartet...", "info")
    return redirect(url_for("dashboard"))


@app.route("/api/bulk-delete-senders", methods=["POST"])
def api_bulk_delete_senders():
    """Loescht alle Mails von mehreren ausgewaehlten Absendern."""
    data = request.get_json()
    sender_ids = data.get("sender_ids", [])

    if not sender_ids:
        return jsonify({"error": "Keine Absender ausgewaehlt"}), 400

    senders = Sender.query.filter(Sender.id.in_(sender_ids)).all()
    if not senders:
        return jsonify({"error": "Absender nicht gefunden"}), 404

    total_mails = sum(s.mail_count for s in senders)
    sender_emails = [(s.id, s.email) for s in senders]

    task_id = str(uuid.uuid4())[:8]
    task_manager.create_task(task_id, f"Bulk-Delete: {len(senders)} Absender, {total_mails} Mails")

    def run_delete():
        with app.app_context():
            try:
                accounts = ImapAccount.query.all()
                total_deleted = 0

                for idx, (sid, semail) in enumerate(sender_emails):
                    task_manager.update(task_id, idx, len(sender_emails),
                                        f"Loesche {semail}...",
                                        f"{idx+1}/{len(sender_emails)} Absender, {total_deleted} geloescht")

                    if accounts:
                        for account in accounts:
                            result = delete_mails_by_sender(account.id, semail)
                            total_deleted += result.get("deleted", 0)
                    else:
                        Mail.query.filter_by(sender_email=semail).update(
                            {"is_deleted": True}, synchronize_session=False
                        )
                        db.session.commit()

                    s = Sender.query.get(sid)
                    if s:
                        s.is_blocked = True
                        s.mail_count = 0
                        db.session.commit()

                task_manager.finish(task_id, {"deleted": total_deleted, "senders": len(sender_emails)})
                _update_sender_stats()
            except Exception as e:
                task_manager.fail(task_id, str(e))

    threading.Thread(target=run_delete, daemon=True).start()
    return jsonify({"task_id": task_id, "total_mails": total_mails})


# ─── Analyse ─────────────────────────────────────────────────────────────────


@app.route("/analyse")
def analyse():
    """Groessenanalyse: Groesste Mails, Speicher pro Absender, Heatmap."""
    from sqlalchemy import func

    total_size = db.session.query(func.sum(Mail.mail_size)).filter(
        Mail.is_deleted == False
    ).scalar() or 0
    total_mails = Mail.query.filter_by(is_deleted=False).count()

    biggest_mails = (
        Mail.query.filter(Mail.is_deleted == False, Mail.mail_size > 0)
        .order_by(Mail.mail_size.desc())
        .limit(50)
        .all()
    )

    sender_sizes = (
        db.session.query(
            Sender.email,
            Sender.display_name,
            func.sum(Mail.mail_size).label("total_size"),
            func.count(Mail.id).label("mail_count"),
            func.avg(Mail.mail_size).label("avg_size"),
        )
        .join(Mail, Mail.sender_id == Sender.id)
        .filter(Mail.is_deleted == False)
        .group_by(Sender.id)
        .order_by(func.sum(Mail.mail_size).desc())
        .limit(30)
        .all()
    )

    folder_sizes = (
        db.session.query(
            Mail.imap_folder,
            func.sum(Mail.mail_size).label("total_size"),
            func.count(Mail.id).label("mail_count"),
        )
        .filter(Mail.is_deleted == False)
        .group_by(Mail.imap_folder)
        .order_by(func.sum(Mail.mail_size).desc())
        .all()
    )

    all_mails = (
        Mail.query.filter(Mail.is_deleted == False, Mail.date != None, Mail.mail_size > 0)
        .with_entities(Mail.date, Mail.mail_size)
        .all()
    )

    from collections import defaultdict
    month_size = defaultdict(lambda: {"tiny": 0, "small": 0, "medium": 0, "large": 0, "huge": 0})
    for mail_date, size in all_mails:
        if not mail_date:
            continue
        month_key = mail_date.strftime("%Y-%m")
        if size < 10240:
            month_size[month_key]["tiny"] += 1
        elif size < 102400:
            month_size[month_key]["small"] += 1
        elif size < 1048576:
            month_size[month_key]["medium"] += 1
        elif size < 5242880:
            month_size[month_key]["large"] += 1
        else:
            month_size[month_key]["huge"] += 1

    heatmap_months = sorted(month_size.keys())
    heatmap = [{"month": m, **month_size[m]} for m in heatmap_months]

    return render_template(
        "analyse.html",
        total_size=total_size,
        total_mails=total_mails,
        biggest_mails=biggest_mails,
        sender_sizes=sender_sizes,
        folder_sizes=folder_sizes,
        heatmap=heatmap,
    )


# ─── Ordner-Verwaltung ────────────────────────────────────────────────────────


@app.route("/ordner")
def ordner():
    """Ordner-Uebersicht mit Live-IMAP-Daten."""
    return render_template("ordner.html")


@app.route("/api/ordner/scan")
def api_ordner_scan():
    """Startet Ordner-Scan als Background-Task, gibt task_id zurueck."""
    active = task_manager.get_active()
    for t in active:
        if t["description"].startswith("Ordner-Scan"):
            return jsonify({"task_id": t["id"]})

    account = ImapAccount.query.first()
    if not account:
        return jsonify({"error": "Kein Account konfiguriert"}), 400

    task_id = str(uuid.uuid4())[:8]
    task_manager.create_task(task_id, "Ordner-Scan")

    def run_scan():
        import re as _re
        import email.header
        from email.utils import parsedate_to_datetime

        def decode_imap_header(raw_value):
            """Dekodiert MIME-encoded Header wie =?UTF-8?Q?...?="""
            if not raw_value or raw_value == "?":
                return raw_value
            try:
                parts = email.header.decode_header(raw_value)
                result = []
                for part, charset in parts:
                    if isinstance(part, bytes):
                        result.append(part.decode(charset or "utf-8", errors="replace"))
                    else:
                        result.append(part)
                return " ".join(result)
            except Exception:
                return raw_value

        def decode_utf7(s):
            """Dekodiert modified UTF-7 (IMAP Ordnernamen wie Grundst&APw-cke)."""
            if '&' not in s or '-' not in s:
                return s
            try:
                result = []
                i = 0
                while i < len(s):
                    if s[i] == '&' and '-' in s[i:]:
                        end = s.index('-', i + 1)
                        if end == i + 1:
                            result.append('&')
                        else:
                            encoded = '+' + s[i+1:end].replace(',', '/')
                            result.append(encoded.encode('ascii').decode('utf-7'))
                        i = end + 1
                    else:
                        result.append(s[i])
                        i += 1
                return ''.join(result)
            except Exception:
                return s

        def format_date(raw_date):
            """Konvertiert RFC2822 Datum zu DD.MM.YYYY HH:MM"""
            if not raw_date or raw_date == "?":
                return raw_date
            try:
                dt = parsedate_to_datetime(raw_date)
                return dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                return raw_date[:20]

        with app.app_context():
            try:
                from imap_client import get_imap_connection
                account_local = ImapAccount.query.first()
                conn = get_imap_connection(account_local)

                task_manager.update(task_id, 0, 10, "Ordnerliste laden...", "")

                _, folder_list = conn.list()
                folders = []
                for entry in folder_list:
                    if isinstance(entry, bytes):
                        decoded = entry.decode("utf-8", errors="replace")
                        parts = decoded.split('"')
                        name = parts[-2] if len(parts) >= 2 else decoded.split()[-1]
                        folders.append(name)

                folder_info = []
                skip_system = {"[Gmail]"}

                for idx, folder in enumerate(folders):
                    if folder in skip_system:
                        continue
                    try:
                        task_manager.update(task_id, idx, len(folders),
                                            f"Scanne {folder}...",
                                            f"{idx}/{len(folders)} Ordner")

                        status, _ = conn.select(f'"{folder}"', readonly=True)
                        if status != "OK":
                            continue

                        _, nums = conn.uid("SEARCH", None, "ALL")
                        uids = set(nums[0].split()) if nums[0] else set()
                        count = len(uids)

                        info = {"name": decode_utf7(folder), "raw_name": folder, "count": count}
                        folder_info.append(info)

                    except Exception:
                        pass

                # ─── Verwaiste Mails per X-GM-LABELS finden ───
                task_manager.update(task_id, len(folders), len(folders) + 2,
                                    "Suche verwaiste Mails per Labels...", "")

                orphan_mails = []
                orphan_sizes = []
                all_mail_folder = None

                for f in folder_info:
                    if f["name"].startswith("[Gmail]/Alle Nachrichten") or f["name"].startswith("[Gmail]/All Mail"):
                        all_mail_folder = f["name"]
                        break

                orphan_count = 0
                if all_mail_folder:
                    conn.select(f'"{all_mail_folder}"', readonly=True)
                    _, nums = conn.uid("SEARCH", None, "ALL")
                    all_uids = nums[0].split() if nums[0] else []

                    task_manager.update(task_id, len(folders) + 1, len(folders) + 2,
                                        f"Pruefe Labels fuer {len(all_uids)} Mails...", "")

                    batch_size = 100
                    for start in range(0, len(all_uids), batch_size):
                        batch = all_uids[start:start + batch_size]
                        uid_str = b",".join(batch).decode()
                        try:
                            _, data = conn.uid("FETCH", uid_str, "(X-GM-LABELS RFC822.SIZE)")
                            for item in data:
                                # Daten koennen bytes ODER tuple sein
                                raw = ""
                                if isinstance(item, bytes):
                                    raw = item.decode("utf-8", errors="replace")
                                elif isinstance(item, tuple):
                                    raw = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
                                else:
                                    continue

                                uid_m = _re.search(r"UID (\d+)", raw)
                                labels_m = _re.search(r"X-GM-LABELS \(([^)]*)\)", raw)
                                size_m = _re.search(r"RFC822\.SIZE (\d+)", raw)

                                if not uid_m or not labels_m:
                                    continue

                                uid_val = uid_m.group(1)
                                labels_raw = labels_m.group(1).strip()
                                size_val = int(size_m.group(1)) if size_m else 0

                                # Labels parsen
                                labels = set()
                                # Quoted labels: "\\Inbox", "\\Sent", "Scheidung"
                                for lb in _re.findall(r'"([^"]*)"', labels_raw):
                                    labels.add(lb)
                                # Unquoted labels
                                remaining = _re.sub(r'"[^"]*"', '', labels_raw).strip()
                                for lb in remaining.split():
                                    if lb:
                                        labels.add(lb)

                                # Verwaist = hat weder \Inbox, \Sent, noch ein User-Label
                                has_inbox = any("Inbox" in lb for lb in labels)
                                has_sent = any("Sent" in lb for lb in labels)
                                user_labels = [lb for lb in labels if not lb.startswith("\\")]

                                if not has_inbox and not has_sent and not user_labels:
                                    orphan_count += 1
                                    orphan_sizes.append((uid_val, size_val))
                        except Exception:
                            pass

                        if start % 500 == 0 and start > 0:
                            task_manager.update(task_id, len(folders) + 1, len(folders) + 2,
                                                f"Pruefe Labels... {start}/{len(all_uids)}",
                                                f"{orphan_count} verwaiste gefunden")

                    # Top 100 nach Groesse
                    orphan_sizes.sort(key=lambda x: x[1], reverse=True)
                    top_orphans = orphan_sizes[:100]

                    # Headers fuer Top 100
                    for uid, size in top_orphans:
                        try:
                            _, hdata = conn.uid("FETCH", uid, "(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])")
                            header = ""
                            if hdata:
                                for part in hdata:
                                    if isinstance(part, tuple) and len(part) > 1:
                                        header = part[1].decode("utf-8", errors="replace")
                                        break
                            lines = header.strip().split("\n")
                            subject = next((l[8:].strip() for l in lines if l.lower().startswith("subject:")), "?")
                            from_addr = next((l[5:].strip() for l in lines if l.lower().startswith("from:")), "?")
                            date = next((l[5:].strip() for l in lines if l.lower().startswith("date:")), "?")

                            orphan_mails.append({
                                "uid": uid, "size": size,
                                "subject": decode_imap_header(subject)[:80],
                                "from": decode_imap_header(from_addr)[:60],
                                "date": format_date(date),
                            })
                        except Exception:
                            pass

                # ─── Inbox Mails laden ───
                inbox_mails = []
                inbox_sizes = []
                inbox_count = 0
                inbox_total_size = 0

                for f in folder_info:
                    if f["name"] == "INBOX":
                        inbox_count = f["count"]
                        break

                if inbox_count > 0:
                    task_manager.update(task_id, len(folders) + 2, len(folders) + 4,
                                        "Lade Inbox-Mails...", "")
                    conn.select('"INBOX"', readonly=True)
                    _, nums = conn.uid("SEARCH", None, "ALL")
                    inbox_uids = nums[0].split() if nums[0] else []

                    inbox_labels_map = {}  # uid -> labels
                    batch_size = 100
                    for start in range(0, len(inbox_uids), batch_size):
                        batch = inbox_uids[start:start + batch_size]
                        uid_str = b",".join(batch).decode()
                        try:
                            _, data = conn.uid("FETCH", uid_str, "(RFC822.SIZE X-GM-LABELS)")
                            for item in data:
                                raw = ""
                                if isinstance(item, bytes):
                                    raw = item.decode("utf-8", errors="replace")
                                elif isinstance(item, tuple):
                                    raw = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
                                else:
                                    continue
                                uid_m = _re.search(r"UID (\d+)", raw)
                                size_m = _re.search(r"RFC822\.SIZE (\d+)", raw)
                                labels_m = _re.search(r"X-GM-LABELS \(([^)]*)\)", raw)
                                if uid_m and size_m:
                                    uid_val = uid_m.group(1)
                                    inbox_sizes.append((uid_val, int(size_m.group(1))))
                                    if labels_m:
                                        labels = []
                                        for lb in _re.findall(r'"([^"]*)"', labels_m.group(1)):
                                            if not lb.startswith("\\"):
                                                labels.append(decode_utf7(lb))
                                        inbox_labels_map[uid_val] = labels
                        except Exception:
                            pass

                    inbox_total_size = sum(s for _, s in inbox_sizes)
                    inbox_sizes.sort(key=lambda x: x[1], reverse=True)
                    top_inbox = inbox_sizes[:100]

                    for uid, size in top_inbox:
                        try:
                            _, hdata = conn.uid("FETCH", uid, "(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])")
                            header = ""
                            if hdata:
                                for part in hdata:
                                    if isinstance(part, tuple) and len(part) > 1:
                                        header = part[1].decode("utf-8", errors="replace")
                                        break
                            lines = header.strip().split("\n")
                            subject = next((l[8:].strip() for l in lines if l.lower().startswith("subject:")), "?")
                            from_addr = next((l[5:].strip() for l in lines if l.lower().startswith("from:")), "?")
                            date = next((l[5:].strip() for l in lines if l.lower().startswith("date:")), "?")

                            inbox_mails.append({
                                "uid": uid, "size": size,
                                "subject": decode_imap_header(subject)[:80],
                                "from": decode_imap_header(from_addr)[:60],
                                "date": format_date(date),
                                "labels": inbox_labels_map.get(uid, []),
                            })
                        except Exception:
                            pass

                # ─── Gesendete Mails laden ───
                sent_mails = []
                sent_sizes = []
                sent_count = 0
                sent_total_size = 0
                sent_folder = None

                for f in folder_info:
                    if "Gesendet" in f["name"] or "Sent" in f["name"]:
                        sent_folder = f["name"]
                        sent_count = f["count"]
                        break

                if sent_folder:
                    task_manager.update(task_id, len(folders) + 2, len(folders) + 3,
                                        f"Lade Gesendet-Mails aus {sent_folder}...", "")
                    conn.select(f'"{sent_folder}"', readonly=True)
                    _, nums = conn.uid("SEARCH", None, "ALL")
                    sent_uids = nums[0].split() if nums[0] else []

                    sent_labels_map = {}
                    batch_size = 100
                    for start in range(0, len(sent_uids), batch_size):
                        batch = sent_uids[start:start + batch_size]
                        uid_str = b",".join(batch).decode()
                        try:
                            _, data = conn.uid("FETCH", uid_str, "(RFC822.SIZE X-GM-LABELS)")
                            for item in data:
                                raw = ""
                                if isinstance(item, bytes):
                                    raw = item.decode("utf-8", errors="replace")
                                elif isinstance(item, tuple):
                                    raw = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
                                else:
                                    continue
                                uid_m = _re.search(r"UID (\d+)", raw)
                                size_m = _re.search(r"RFC822\.SIZE (\d+)", raw)
                                labels_m = _re.search(r"X-GM-LABELS \(([^)]*)\)", raw)
                                if uid_m and size_m:
                                    uid_val = uid_m.group(1)
                                    sent_sizes.append((uid_val, int(size_m.group(1))))
                                    if labels_m:
                                        labels = []
                                        for lb in _re.findall(r'"([^"]*)"', labels_m.group(1)):
                                            if not lb.startswith("\\"):
                                                labels.append(decode_utf7(lb))
                                        sent_labels_map[uid_val] = labels
                        except Exception:
                            pass

                    sent_total_size = sum(s for _, s in sent_sizes)
                    sent_sizes.sort(key=lambda x: x[1], reverse=True)
                    top_sent = sent_sizes[:100]

                    for uid, size in top_sent:
                        try:
                            _, hdata = conn.uid("FETCH", uid, "(BODY[HEADER.FIELDS (TO SUBJECT DATE)])")
                            header = ""
                            if hdata:
                                for part in hdata:
                                    if isinstance(part, tuple) and len(part) > 1:
                                        header = part[1].decode("utf-8", errors="replace")
                                        break
                            lines = header.strip().split("\n")
                            subject = next((l[8:].strip() for l in lines if l.lower().startswith("subject:")), "?")
                            to_addr = next((l[3:].strip() for l in lines if l.lower().startswith("to:")), "?")
                            date = next((l[5:].strip() for l in lines if l.lower().startswith("date:")), "?")

                            sent_mails.append({
                                "uid": uid, "size": size,
                                "subject": decode_imap_header(subject)[:80],
                                "to": decode_imap_header(to_addr)[:60],
                                "date": format_date(date),
                                "labels": sent_labels_map.get(uid, []),
                            })
                        except Exception:
                            pass

                conn.logout()

                total_orphan_size = sum(s for _, s in orphan_sizes)

                result = {
                    "folders": sorted(folder_info, key=lambda x: x["count"], reverse=True),
                    "orphan_count": orphan_count,
                    "orphan_total_size": total_orphan_size,
                    "orphan_mails": orphan_mails,
                    "all_mail_folder": all_mail_folder,
                    "sent_mails": sent_mails,
                    "sent_count": sent_count,
                    "sent_total_size": sent_total_size,
                    "inbox_mails": inbox_mails,
                    "inbox_count": inbox_count,
                    "inbox_total_size": inbox_total_size,
                }

                task_manager.finish(task_id, result)

            except Exception as e:
                task_manager.fail(task_id, str(e))

    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/ordner/more")
def api_ordner_more():
    """Laedt weitere Mails fuer Inbox/Gesendet ab offset."""
    import re as _re
    import email.header
    from email.utils import parsedate_to_datetime

    mail_type = request.args.get("type", "inbox")
    offset = request.args.get("offset", 0, type=int)
    limit = 100

    def decode_imap_header(raw_value):
        if not raw_value or raw_value == "?":
            return raw_value
        try:
            parts = email.header.decode_header(raw_value)
            result = []
            for part, charset in parts:
                if isinstance(part, bytes):
                    result.append(part.decode(charset or "utf-8", errors="replace"))
                else:
                    result.append(part)
            return " ".join(result)
        except Exception:
            return raw_value

    def decode_utf7(s):
        try:
            result = []
            i = 0
            while i < len(s):
                if s[i] == '&' and '-' in s[i:]:
                    end = s.index('-', i + 1)
                    if end == i + 1:
                        result.append('&')
                    else:
                        encoded = '+' + s[i+1:end].replace(',', '/')
                        result.append(encoded.encode('ascii').decode('utf-7'))
                    i = end + 1
                else:
                    result.append(s[i])
                    i += 1
            return ''.join(result)
        except Exception:
            return s

    def format_date(raw_date):
        if not raw_date or raw_date == "?":
            return raw_date
        try:
            dt = parsedate_to_datetime(raw_date)
            return dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return raw_date[:20]

    account = ImapAccount.query.first()
    if not account:
        return jsonify({"error": "Kein Account"}), 400

    try:
        from imap_client import get_imap_connection
        conn = get_imap_connection(account)

        if mail_type == "inbox":
            folder = "INBOX"
            header_field = "FROM"
        else:
            folder = "[Gmail]/Gesendet"
            header_field = "TO"

        conn.select(f'"{folder}"', readonly=True)
        _, nums = conn.uid("SEARCH", None, "ALL")
        all_uids = nums[0].split() if nums[0] else []

        # Groessen + Labels holen
        sizes = []
        labels_map = {}
        batch_size = 100
        for start in range(0, len(all_uids), batch_size):
            batch = all_uids[start:start + batch_size]
            uid_str = b",".join(batch).decode()
            try:
                _, data = conn.uid("FETCH", uid_str, "(RFC822.SIZE X-GM-LABELS)")
                for item in data:
                    raw = ""
                    if isinstance(item, bytes):
                        raw = item.decode("utf-8", errors="replace")
                    elif isinstance(item, tuple):
                        raw = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
                    else:
                        continue
                    uid_m = _re.search(r"UID (\d+)", raw)
                    size_m = _re.search(r"RFC822\.SIZE (\d+)", raw)
                    labels_m = _re.search(r"X-GM-LABELS \(([^)]*)\)", raw)
                    if uid_m and size_m:
                        uid_val = uid_m.group(1)
                        sizes.append((uid_val, int(size_m.group(1))))
                        if labels_m:
                            labels = []
                            for lb in _re.findall(r'"([^"]*)"', labels_m.group(1)):
                                if not lb.startswith("\\"):
                                    labels.append(decode_utf7(lb))
                            labels_map[uid_val] = labels
            except Exception:
                pass

        sizes.sort(key=lambda x: x[1], reverse=True)
        page = sizes[offset:offset + limit]

        mails = []
        for uid, size in page:
            try:
                _, hdata = conn.uid("FETCH", uid, f"(BODY[HEADER.FIELDS ({header_field} SUBJECT DATE)])")
                header = ""
                if hdata:
                    for part in hdata:
                        if isinstance(part, tuple) and len(part) > 1:
                            header = part[1].decode("utf-8", errors="replace")
                            break
                lines = header.strip().split("\n")
                subject = next((l[8:].strip() for l in lines if l.lower().startswith("subject:")), "?")
                date = next((l[5:].strip() for l in lines if l.lower().startswith("date:")), "?")

                entry = {
                    "uid": uid, "size": size,
                    "subject": decode_imap_header(subject)[:80],
                    "date": format_date(date),
                    "labels": labels_map.get(uid, []),
                }

                if mail_type == "inbox":
                    from_addr = next((l[5:].strip() for l in lines if l.lower().startswith("from:")), "?")
                    entry["from"] = decode_imap_header(from_addr)[:60]
                else:
                    to_addr = next((l[3:].strip() for l in lines if l.lower().startswith("to:")), "?")
                    entry["to"] = decode_imap_header(to_addr)[:60]

                mails.append(entry)
            except Exception:
                pass

        conn.logout()
        return jsonify({"mails": mails, "total": len(sizes)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mail/move", methods=["POST"])
def api_mail_move():
    """Verschiebt Mails per Gmail X-GM-LABELS (Label hinzufuegen + Quell-Label entfernen)."""
    data = request.get_json()
    uids = data.get("uids", [])
    source_folder = data.get("source_folder", "[Gmail]/Alle Nachrichten")
    target_folder = data.get("target_folder")

    if not uids or not target_folder:
        return jsonify({"error": "UIDs und Zielordner benoetigt"}), 400

    account = ImapAccount.query.first()
    if not account:
        return jsonify({"error": "Kein Account konfiguriert"}), 400

    try:
        from imap_client import get_imap_connection
        conn = get_imap_connection(account)
        conn.select(f'"{source_folder}"')

        moved = 0
        errors = []

        is_trash = target_folder in ("[Gmail]/Papierkorb", "[Gmail]/Trash", "[Gmail]/Bin")
        is_inbox = target_folder == "INBOX"

        if is_trash:
            uid_str = ",".join(str(u) for u in uids)
            try:
                conn.uid("MOVE", uid_str, f'"{target_folder}"')
                moved = len(uids)
            except Exception:
                for uid in uids:
                    try:
                        conn.uid("MOVE", str(uid), f'"{target_folder}"')
                        moved += 1
                    except Exception as e:
                        errors.append(f"UID {uid}: {e}")
        else:
            # Gmail X-GM-LABELS:
            # System-Labels wie \Inbox OHNE Anfuehrungszeichen
            # User-Labels wie "Scheidung" MIT Anfuehrungszeichen
            if is_inbox:
                label_arg = "\\Inbox"
            else:
                label_arg = f'"{target_folder}"'

            # Quell-Label bestimmen um es zu entfernen
            remove_label = None
            if source_folder == "INBOX":
                remove_label = "\\Inbox"

            for uid in uids:
                try:
                    # Ziel-Label hinzufuegen
                    conn.uid("STORE", str(uid), "+X-GM-LABELS", label_arg)
                    # Quell-Label entfernen (z.B. \Inbox)
                    if remove_label:
                        conn.uid("STORE", str(uid), "-X-GM-LABELS", remove_label)
                    moved += 1
                except Exception as e:
                    errors.append(f"UID {uid}: {e}")

        conn.logout()
        return jsonify({"moved": moved, "errors": errors})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mail/archive", methods=["POST"])
def api_mail_archive():
    """Archiviert Mails: MOVE aus INBOX nach Alle Nachrichten."""
    data = request.get_json()
    uids = data.get("uids", [])

    if not uids:
        return jsonify({"error": "Keine UIDs angegeben"}), 400

    account = ImapAccount.query.first()
    if not account:
        return jsonify({"error": "Kein Account konfiguriert"}), 400

    try:
        from imap_client import get_imap_connection
        conn = get_imap_connection(account)
        conn.select('"INBOX"')

        archived = 0
        errors = []

        # Alle Nachrichten Ordner finden
        all_mail = "[Gmail]/Alle Nachrichten"

        for uid in uids:
            try:
                conn.uid("MOVE", str(uid), f'"{all_mail}"')
                archived += 1
            except Exception as e:
                errors.append(f"UID {uid}: {e}")

        conn.logout()
        return jsonify({"archived": archived, "errors": errors})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mail/preview/<uid>")
def api_mail_preview(uid):
    """Holt Mail-Vorschau via IMAP – parst MIME korrekt, listet Anhaenge."""
    import email
    import email.header
    import re as _re

    folder = request.args.get("folder", "[Gmail]/Alle Nachrichten")
    account = ImapAccount.query.first()
    if not account:
        return jsonify({"error": "Kein Account"}), 400

    try:
        from imap_client import get_imap_connection
        conn = get_imap_connection(account)
        conn.select(f'"{folder}"', readonly=True)

        _, data = conn.uid("FETCH", str(uid), "(RFC822 RFC822.SIZE)")

        raw_msg = None
        size = 0
        for item in data:
            if isinstance(item, tuple):
                header_line = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                size_m = _re.search(r"RFC822\.SIZE (\d+)", header_line)
                if size_m:
                    size = int(size_m.group(1))
                if len(item) > 1 and isinstance(item[1], bytes) and len(item[1]) > 50:
                    raw_msg = item[1]

        conn.logout()

        if not raw_msg:
            return jsonify({"error": "Mail konnte nicht geladen werden"}), 404

        msg = email.message_from_bytes(raw_msg)

        def decode_hdr(val):
            if not val:
                return ""
            parts = email.header.decode_header(val)
            result = []
            for part, charset in parts:
                if isinstance(part, bytes):
                    result.append(part.decode(charset or "utf-8", errors="replace"))
                else:
                    result.append(part)
            return " ".join(result)

        subject = decode_hdr(msg.get("Subject", ""))
        from_addr = decode_hdr(msg.get("From", ""))
        to_addr = decode_hdr(msg.get("To", ""))
        date = decode_hdr(msg.get("Date", ""))

        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition") or "")
                filename = part.get_filename()

                if filename or "attachment" in disposition.lower():
                    att_size = len(part.get_payload(decode=True) or b"")
                    attachments.append({
                        "name": decode_hdr(filename) if filename else "unbenannt",
                        "type": content_type,
                        "size": att_size,
                    })
                elif content_type == "text/plain" and not body_text:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_text = payload.decode(charset, errors="replace")
                elif content_type == "text/html" and not body_html:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_html = payload.decode(charset, errors="replace")
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if content_type == "text/plain":
                    body_text = text
                elif content_type == "text/html":
                    body_html = text

        if body_text:
            display_body = body_text[:3000]
        elif body_html:
            clean = _re.sub(r"<style[^>]*>.*?</style>", "", body_html, flags=_re.DOTALL)
            clean = _re.sub(r"<script[^>]*>.*?</script>", "", clean, flags=_re.DOTALL)
            clean = _re.sub(r"<[^>]+>", " ", clean)
            clean = _re.sub(r"&nbsp;", " ", clean)
            clean = _re.sub(r"&amp;", "&", clean)
            clean = _re.sub(r"&lt;", "<", clean)
            clean = _re.sub(r"&gt;", ">", clean)
            clean = _re.sub(r"\s+", " ", clean).strip()
            display_body = clean[:3000]
        else:
            display_body = "(kein Textinhalt)"

        return jsonify({
            "uid": uid,
            "subject": subject,
            "from": from_addr,
            "to": to_addr,
            "date": date,
            "size": size,
            "body": display_body,
            "attachments": attachments,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/folder/create", methods=["POST"])
def api_folder_create():
    """Legt einen neuen IMAP-Ordner an."""
    data = request.get_json()
    folder_name = data.get("name", "").strip()

    if not folder_name:
        return jsonify({"error": "Ordnername benoetigt"}), 400

    account = ImapAccount.query.first()
    if not account:
        return jsonify({"error": "Kein Account konfiguriert"}), 400

    try:
        from imap_client import get_imap_connection
        conn = get_imap_connection(account)
        status, msg = conn.create(f'"{folder_name}"')
        conn.logout()

        if status == "OK":
            return jsonify({"success": True, "folder": folder_name})
        else:
            return jsonify({"error": f"Konnte Ordner nicht anlegen: {msg}"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=False, port=config.PORT, host="0.0.0.0", use_reloader=False)
