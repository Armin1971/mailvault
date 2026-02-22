"""MailVault Scorer – Regelbasiertes Scoring mit ML-Interface."""

import json
import re
from abc import ABC, abstractmethod
from models import db, Mail, Sender
import config


class BaseScorer(ABC):
    """Abstrakte Basis für Scoring-Module (ML-ready)."""

    @abstractmethod
    def score(self, mail):
        """Berechnet einen Score für eine Mail.
        Gibt (score_modifier, details_dict) zurück."""
        pass


class HeaderScorer(BaseScorer):
    """Scoring basierend auf Mail-Headern."""

    def score(self, mail):
        details = {}
        modifier = 0
        weights = config.SCORING_WEIGHTS

        if mail.has_unsubscribe:
            modifier += weights["has_unsubscribe_header"]
            details["unsubscribe_header"] = weights["has_unsubscribe_header"]

        if mail.has_list_header:
            modifier += weights["has_list_header"]
            details["list_header"] = weights["has_list_header"]

        if mail.is_noreply:
            modifier += weights["is_noreply_sender"]
            details["noreply_sender"] = weights["is_noreply_sender"]

        # Reply-Chain erkennen
        if mail.subject and re.match(r"^(Re|Aw|Fwd|Wg):", mail.subject, re.IGNORECASE):
            modifier += weights["is_reply_chain"]
            details["reply_chain"] = weights["is_reply_chain"]

        return modifier, details


class KeywordScorer(BaseScorer):
    """Scoring basierend auf Keywords im Body."""

    def score(self, mail):
        details = {}
        modifier = 0
        weights = config.SCORING_WEIGHTS
        text = (mail.body_preview or "").lower()

        if not text:
            return 0, {}

        # Spam/Werbe-Keywords
        spam_hits = 0
        for kw in config.SPAM_KEYWORDS_DE:
            if kw.lower() in text:
                spam_hits += 1
        if spam_hits > 0:
            mod = spam_hits * weights["spam_keywords_per_hit"]
            # Cap bei -30
            mod = max(mod, -30)
            modifier += mod
            details["spam_keywords"] = {"count": spam_hits, "modifier": mod}

        # Persönliche Keywords
        personal_hits = 0
        for kw in config.PERSONAL_KEYWORDS_DE:
            if kw.lower() in text:
                personal_hits += 1
        if personal_hits > 0:
            mod = personal_hits * weights["personal_keywords_per_hit"]
            mod = min(mod, 25)
            modifier += mod
            details["personal_keywords"] = {"count": personal_hits, "modifier": mod}

        return modifier, details


class FrequencyScorer(BaseScorer):
    """Scoring basierend auf Absender-Frequenz."""

    def score(self, mail):
        details = {}
        modifier = 0
        weights = config.SCORING_WEIGHTS

        sender = Sender.query.filter_by(email=mail.sender_email).first()
        if not sender:
            return 0, {}

        count = sender.mail_count
        if count > 200:
            modifier += weights["very_high_frequency_penalty"]
            details["frequency"] = {
                "count": count,
                "modifier": weights["very_high_frequency_penalty"],
            }
        elif count > 50:
            modifier += weights["high_frequency_sender_penalty"]
            details["frequency"] = {
                "count": count,
                "modifier": weights["high_frequency_sender_penalty"],
            }

        return modifier, details


class StructureScorer(BaseScorer):
    """Scoring basierend auf Mail-Struktur."""

    def score(self, mail):
        details = {}
        modifier = 0
        weights = config.SCORING_WEIGHTS

        if mail.has_html:
            modifier += weights["high_html_ratio_penalty"]
            details["html_mail"] = weights["high_html_ratio_penalty"]

        if mail.body_length and mail.body_length < 50:
            modifier += weights["very_short_body_penalty"]
            details["short_body"] = weights["very_short_body_penalty"]

        return modifier, details


# --- Haupt-Scorer ---

# Registry aller aktiven Scorer
ACTIVE_SCORERS = [
    HeaderScorer(),
    KeywordScorer(),
    FrequencyScorer(),
    StructureScorer(),
]


def calculate_score(mail):
    """Berechnet den Gesamt-Score für eine Mail."""
    total = config.BASE_SCORE
    all_details = {}

    for scorer in ACTIVE_SCORERS:
        modifier, details = scorer.score(mail)
        total += modifier
        all_details.update(details)

    # Clamp zwischen 0 und 100
    total = max(0, min(100, total))

    return total, all_details


def score_all_mails():
    """Berechnet Scores für alle Mails in der DB."""
    mails = Mail.query.filter_by(is_deleted=False).all()
    count = 0
    for mail in mails:
        score, details = calculate_score(mail)
        mail.score = score
        mail.score_details = json.dumps(details, ensure_ascii=False)
        count += 1
        if count % 100 == 0:
            db.session.commit()

    db.session.commit()

    # Sender-Scores updaten
    _update_sender_scores()
    # Kategorien ableiten
    _categorize_senders()

    return count


def _update_sender_scores():
    """Berechnet durchschnittliche Scores pro Sender."""
    senders = Sender.query.all()
    for sender in senders:
        mails = Mail.query.filter_by(sender_id=sender.id, is_deleted=False).all()
        if mails:
            scores = [m.score for m in mails if m.score is not None]
            if scores:
                sender.avg_score = round(sum(scores) / len(scores), 1)
    db.session.commit()


def _categorize_senders():
    """Kategorisiert Sender basierend auf ihren Mails."""
    senders = Sender.query.all()
    for sender in senders:
        mails = Mail.query.filter_by(sender_id=sender.id, is_deleted=False).all()
        if not mails:
            continue

        unsub_ratio = sum(1 for m in mails if m.has_unsubscribe) / len(mails)
        noreply = any(m.is_noreply for m in mails)

        if unsub_ratio > 0.5 and sender.mail_count > 5:
            sender.category = "newsletter"
        elif noreply and sender.avg_score < 30:
            sender.category = "commercial"
        elif sender.avg_score > 60:
            sender.category = "personal"
        elif unsub_ratio > 0.3:
            sender.category = "transactional"
        else:
            sender.category = "unknown"

    db.session.commit()


# --- ML-Scorer Platzhalter ---

class MLScorer(BaseScorer):
    """Platzhalter für zukünftiges ML-basiertes Scoring.

    Ideen für spätere Implementierung:
    - TF-IDF + Logistic Regression auf manuell gelabelten Mails
    - scikit-learn Pipeline mit CountVectorizer
    - Training: User markiert Mails als relevant/irrelevant → Model lernt

    Usage:
        ml_scorer = MLScorer()
        ml_scorer.train(labeled_mails)
        ACTIVE_SCORERS.append(ml_scorer)
    """

    def __init__(self):
        self.model = None
        self.vectorizer = None

    def train(self, mails_with_labels):
        """Trainiert das ML-Modell mit gelabelten Mails.

        Args:
            mails_with_labels: Liste von (body_text, is_relevant_bool) Tupeln
        """
        # TODO: Implementieren mit scikit-learn
        # from sklearn.feature_extraction.text import TfidfVectorizer
        # from sklearn.linear_model import LogisticRegression
        pass

    def score(self, mail):
        if not self.model:
            return 0, {}
        # TODO: Prediction implementieren
        return 0, {"ml_score": "not_trained"}
