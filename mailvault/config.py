"""MailVault Konfiguration."""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Datenbank ---
DATABASE_URI = f"sqlite:///{os.path.join(BASE_DIR, 'mailvault.db')}"

# --- Thunderbird Profil-Pfad ---
# Typisch: ~/.thunderbird/<profil>.default-release/ImapMail/<server>/
THUNDERBIRD_PROFILE = os.path.expanduser("~/.thunderbird")

# --- IMAP-Konfiguration ---
# Wird über das Web-UI konfiguriert und in der DB gespeichert.
# Fallback-Defaults:
IMAP_DEFAULTS = {
    "server": "",
    "port": 993,
    "use_ssl": True,
    "username": "",
    "password": "",
}

# --- Scoring-Gewichte ---
SCORING_WEIGHTS = {
    # Header-basiert
    "has_unsubscribe_header": -30,
    "has_list_header": -20,
    "is_noreply_sender": -25,
    "is_reply_chain": +15,
    "has_multiple_recipients": -5,

    # Keyword-basiert (Body)
    "spam_keywords_per_hit": -5,
    "personal_keywords_per_hit": +5,

    # Absender-Frequenz
    "high_frequency_sender_penalty": -15,  # > 50 Mails
    "very_high_frequency_penalty": -25,    # > 200 Mails

    # Struktur
    "high_html_ratio_penalty": -10,
    "has_images_penalty": -5,
    "very_short_body_penalty": -5,
}

# Keywords die auf Werbung/Newsletter hindeuten
SPAM_KEYWORDS_DE = [
    "abbestellen", "abmelden", "newsletter", "angebot", "rabatt",
    "gutschein", "sale", "prozent", "sonderaktion", "gratis",
    "kostenlos", "jetzt bestellen", "jetzt kaufen", "jetzt sichern",
    "versandkostenfrei", "schnäppchen", "aktion", "gewinnspiel",
    "limited offer", "act now", "click here", "buy now",
    "unsubscribe", "opt-out", "promotional", "discount",
    "free shipping", "deal", "offer expires", "don't miss",
]

# Keywords die auf persönliche/relevante Mails hindeuten
PERSONAL_KEYWORDS_DE = [
    "hallo", "lieber", "liebe", "hi ", "moin",
    "anbei", "wie besprochen", "zur info", "fyi",
    "termin", "meeting", "besprechung", "telefonat",
    "rechnung", "vertrag", "kündigung", "mahnung",
    "bewerbung", "zusage", "absage", "einladung",
]

# Basis-Score (alle Mails starten hier)
BASE_SCORE = 50

# Flask
SECRET_KEY = os.environ.get("MAILVAULT_SECRET", "dev-key-change-in-production")
DEBUG = True
PORT = 5000
