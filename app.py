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

    if order == "asc":
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
    mail_ids = request.form.getlist("mail_ids", type=int)
    if not mail_ids:
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


# ─── Analyse ─────────────────────────────────────────────────────────────────


@app.route("/analyse")
def analyse():
    """Groessenanalyse: Groesste Mails, Speicher pro Absender, Heatmap."""
    from sqlalchemy import func

    # Gesamtstatistiken
    total_size = db.session.query(func.sum(Mail.mail_size)).filter(
        Mail.is_deleted == False
    ).scalar() or 0
    total_mails = Mail.query.filter_by(is_deleted=False).count()

    # Top 50 groesste Mails
    biggest_mails = (
        Mail.query.filter(Mail.is_deleted == False, Mail.mail_size > 0)
        .order_by(Mail.mail_size.desc())
        .limit(50)
        .all()
    )

    # Groesse pro Absender (Top 30)
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

    # Groesse pro Ordner
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

    # Heatmap-Daten: Mails nach Monat und Groessenkategorie
    heatmap_data = []
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
        if size < 10240:          # < 10 KB
            month_size[month_key]["tiny"] += 1
        elif size < 102400:       # < 100 KB
            month_size[month_key]["small"] += 1
        elif size < 1048576:      # < 1 MB
            month_size[month_key]["medium"] += 1
        elif size < 5242880:      # < 5 MB
            month_size[month_key]["large"] += 1
        else:                     # >= 5 MB
            month_size[month_key]["huge"] += 1

    # Sortiert nach Monat
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


if __name__ == "__main__":
    app.run(debug=config.DEBUG, port=config.PORT, host="0.0.0.0")
