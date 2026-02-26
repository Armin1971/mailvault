"""MailVault Datenbank-Models."""

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class ImapAccount(db.Model):
    """IMAP-Account Konfiguration."""
    __tablename__ = "imap_accounts"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)  # z.B. "GMX Privat"
    server = db.Column(db.String(200), nullable=False)
    port = db.Column(db.Integer, default=993)
    use_ssl = db.Column(db.Boolean, default=True)
    username = db.Column(db.String(200), nullable=False)
    password = db.Column(db.String(200), nullable=False)  # TODO: verschlüsseln
    last_scan = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    mails = db.relationship("Mail", backref="account", lazy="dynamic")


class Sender(db.Model):
    """Eindeutiger Absender."""
    __tablename__ = "senders"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(300), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(300), nullable=True)
    mail_count = db.Column(db.Integer, default=0)
    avg_score = db.Column(db.Float, default=50.0)
    category = db.Column(db.String(50), default="unknown")
    # Kategorien: newsletter, commercial, personal, transactional, unknown
    is_blocked = db.Column(db.Boolean, default=False)
    first_seen = db.Column(db.DateTime, nullable=True)
    last_seen = db.Column(db.DateTime, nullable=True)

    mails = db.relationship("Mail", backref="sender_rel", lazy="dynamic")


class Mail(db.Model):
    """Einzelne Mail."""
    __tablename__ = "mails"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(500), unique=True, nullable=True, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("imap_accounts.id"), nullable=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("senders.id"), nullable=True)

    sender_email = db.Column(db.String(300), index=True)
    sender_name = db.Column(db.String(300))
    subject = db.Column(db.String(500))
    date = db.Column(db.DateTime, nullable=True, index=True)
    folder = db.Column(db.String(200))  # INBOX, Sent, etc.

    # Analyse
    score = db.Column(db.Integer, default=50)
    score_details = db.Column(db.Text, nullable=True)  # JSON mit Einzelwertungen
    body_preview = db.Column(db.Text, nullable=True)  # Erste 500 Zeichen
    body_length = db.Column(db.Integer, default=0)
    mail_size = db.Column(db.Integer, default=0)  # RFC822.SIZE in Bytes
    has_html = db.Column(db.Boolean, default=False)
    has_attachments = db.Column(db.Boolean, default=False)

    # Header-Flags
    has_unsubscribe = db.Column(db.Boolean, default=False)
    has_list_header = db.Column(db.Boolean, default=False)
    is_noreply = db.Column(db.Boolean, default=False)

    # Status
    is_deleted = db.Column(db.Boolean, default=False)
    marked_for_delete = db.Column(db.Boolean, default=False)
    scanned_at = db.Column(db.DateTime, default=datetime.utcnow)

    # IMAP-Referenz für Löschung
    imap_uid = db.Column(db.String(100), nullable=True)
    imap_folder = db.Column(db.String(200), nullable=True)


class ScoringRule(db.Model):
    """Benutzerdefinierte Scoring-Regeln."""
    __tablename__ = "scoring_rules"

    id = db.Column(db.Integer, primary_key=True)
    rule_type = db.Column(db.String(50))  # keyword, sender, domain, header
    pattern = db.Column(db.String(300))
    score_modifier = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
