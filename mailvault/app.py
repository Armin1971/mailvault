"""MailVault – Flask Application."""

import json
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from models import db, Mail, Sender, ImapAccount, ScoringRule
from scanner import scan_thunderbird_profile, scan_imap_account
from scorer import score_all_mails
from imap_client import delete_mails_by_sender, delete_mails_by_ids, test_connection
import config

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = config.SECRET_KEY

db.init_app(app)

with app.app_context():
    db.create_all()


# --- Dashboard ---


@app.route("/")
def dashboard():
    """Hauptübersicht: Statistiken und Absender-Liste."""
    sort = request.args.get("sort", "count")
    order = request.args.get("order", "desc")
    category = request.args.get("category", "all")
    min_score = request.args.get("min_score", type=int)
    max_score = request.args.get("max_score", type=int)

    query = Sender.query.filter(Sender.mail_count > 0)

    if category and category != "all":
        query = query.filter_by(category=category)
    if min_score is not None:
        query = query.filter(Sender.avg_score >= min_score)
    if max_score is not None:
        query = query.filter(Sender.avg_score <= max_score)

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

    # Statistiken
    total_mails = Mail.query.filter_by(is_deleted=False).count()
    total_senders = Sender.query.filter(Sender.mail_count > 0).count()
    avg_score = db.session.query(db.func.avg(Mail.score)).filter(
        Mail.is_deleted == False
    ).scalar() or 0
    low_score_count = Mail.query.filter(
        Mail.score < 30, Mail.is_deleted == False
    ).count()

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


# --- Absender-Detail ---


@app.route("/sender/<int:sender_id>")
def sender_detail(sender_id):
    """Zeigt alle Mails eines Absenders."""
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
        "sender_detail.html", sender=sender, mails=mails,
        current_sort=sort, current_order=order,
    )


# --- Lösch-Aktionen ---


@app.route("/sender/<int:sender_id>/delete-all", methods=["POST"])
def delete_sender_mails(sender_id):
    """Löscht alle Mails eines Absenders."""
    sender = Sender.query.get_or_404(sender_id)

    # Prüfe ob IMAP-Account verfügbar
    accounts = ImapAccount.query.all()
    if accounts:
        try:
            for account in accounts:
                result = delete_mails_by_sender(account.id, sender.email)
                if result["errors"]:
                    for err in result["errors"]:
                        flash(f"Warnung: {err}", "warning")
            flash(
                f'{result["deleted"]} Mails von {sender.email} gelöscht (IMAP)',
                "success",
            )
        except Exception as e:
            flash(f"IMAP-Fehler: {e}", "error")
            # Fallback: Nur lokal markieren
            _mark_deleted_locally(sender.email)
    else:
        _mark_deleted_locally(sender.email)
        flash(
            f"Mails von {sender.email} lokal als gelöscht markiert "
            "(kein IMAP-Account konfiguriert)",
            "info",
        )

    sender.is_blocked = True
    db.session.commit()

    return redirect(url_for("dashboard"))


@app.route("/mails/delete", methods=["POST"])
def delete_selected_mails():
    """Löscht ausgewählte Mails."""
    mail_ids = request.form.getlist("mail_ids", type=int)
    if not mail_ids:
        flash("Keine Mails ausgewählt", "warning")
        return redirect(request.referrer or url_for("dashboard"))

    accounts = ImapAccount.query.all()
    if accounts:
        for account in accounts:
            try:
                result = delete_mails_by_ids(account.id, mail_ids)
                flash(f'{result["deleted"]} Mails gelöscht', "success")
            except Exception as e:
                flash(f"Fehler: {e}", "error")
    else:
        Mail.query.filter(Mail.id.in_(mail_ids)).update(
            {"is_deleted": True}, synchronize_session=False
        )
        db.session.commit()
        flash(f"{len(mail_ids)} Mails lokal als gelöscht markiert", "info")

    return redirect(request.referrer or url_for("dashboard"))


def _mark_deleted_locally(sender_email):
    """Markiert Mails lokal als gelöscht."""
    Mail.query.filter_by(sender_email=sender_email).update(
        {"is_deleted": True}, synchronize_session=False
    )
    db.session.commit()


# --- Scan ---


@app.route("/scan", methods=["GET", "POST"])
def scan():
    """Scan-Seite: Thunderbird-Profil oder IMAP scannen."""
    if request.method == "POST":
        scan_type = request.form.get("scan_type", "thunderbird")

        if scan_type == "thunderbird":
            try:
                stats = scan_thunderbird_profile()
                scored = score_all_mails()
                flash(
                    f'Scan abgeschlossen: {stats["imported"]} Mails importiert, '
                    f'{scored} Mails bewertet, {stats["errors"]} Fehler',
                    "success",
                )
            except Exception as e:
                flash(f"Scan-Fehler: {e}", "error")

        elif scan_type == "imap":
            account_id = request.form.get("account_id", type=int)
            limit = request.form.get("limit", type=int, default=500)
            if account_id:
                try:
                    stats = scan_imap_account(account_id, limit=limit)
                    scored = score_all_mails()
                    flash(
                        f'IMAP-Scan: {stats["imported"]} Mails importiert, '
                        f'{scored} bewertet',
                        "success",
                    )
                except Exception as e:
                    flash(f"IMAP-Fehler: {e}", "error")

        return redirect(url_for("dashboard"))

    accounts = ImapAccount.query.all()
    return render_template("scan.html", accounts=accounts)


# --- Einstellungen ---


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """IMAP-Account Verwaltung."""
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
            flash(f'Account "{account.name}" hinzugefügt', "success")

        elif action == "test_account":
            result = test_connection(
                request.form["server"],
                int(request.form.get("port", 993)),
                "use_ssl" in request.form,
                request.form["username"],
                request.form["password"],
            )
            if result["success"]:
                flash(
                    f'Verbindung erfolgreich! {result["folders"]} Ordner gefunden.',
                    "success",
                )
            else:
                flash(f'Verbindung fehlgeschlagen: {result["error"]}', "error")

        elif action == "delete_account":
            account_id = request.form.get("account_id", type=int)
            account = ImapAccount.query.get(account_id)
            if account:
                db.session.delete(account)
                db.session.commit()
                flash(f'Account "{account.name}" gelöscht', "success")

        return redirect(url_for("settings"))

    accounts = ImapAccount.query.all()
    return render_template("settings.html", accounts=accounts)


# --- API-Endpunkte ---


@app.route("/api/sender/<int:sender_id>/block", methods=["POST"])
def api_block_sender(sender_id):
    """Blockiert/entblockiert einen Sender."""
    sender = Sender.query.get_or_404(sender_id)
    sender.is_blocked = not sender.is_blocked
    db.session.commit()
    return jsonify({"blocked": sender.is_blocked})


@app.route("/api/stats")
def api_stats():
    """Gibt aktuelle Statistiken als JSON zurück."""
    return jsonify(
        {
            "total_mails": Mail.query.filter_by(is_deleted=False).count(),
            "total_senders": Sender.query.filter(Sender.mail_count > 0).count(),
            "by_category": dict(
                db.session.query(Sender.category, db.func.count(Sender.id))
                .filter(Sender.mail_count > 0)
                .group_by(Sender.category)
                .all()
            ),
        }
    )


# --- Bulk-Aktionen ---


@app.route("/bulk/delete-low-score", methods=["POST"])
def bulk_delete_low_score():
    """Löscht alle Mails unter einem bestimmten Score."""
    threshold = request.form.get("threshold", type=int, default=20)
    mails = Mail.query.filter(Mail.score < threshold, Mail.is_deleted == False).all()

    if not mails:
        flash("Keine Mails unter diesem Score gefunden", "info")
        return redirect(url_for("dashboard"))

    mail_ids = [m.id for m in mails]
    accounts = ImapAccount.query.all()

    if accounts:
        for account in accounts:
            try:
                result = delete_mails_by_ids(account.id, mail_ids)
            except Exception as e:
                flash(f"Fehler: {e}", "error")
    else:
        Mail.query.filter(Mail.id.in_(mail_ids)).update(
            {"is_deleted": True}, synchronize_session=False
        )
        db.session.commit()

    flash(f"{len(mail_ids)} Mails mit Score < {threshold} gelöscht", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(debug=config.DEBUG, port=config.PORT, host="0.0.0.0")
