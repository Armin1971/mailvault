"""Fuegt mail_size Spalte zur bestehenden DB hinzu und laedt Groessen nach."""

import sqlite3
import sys

DB_PATH = "/opt/mailvault/mailvault.db"


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Pruefen ob Spalte existiert
    cursor.execute("PRAGMA table_info(mails)")
    columns = [row[1] for row in cursor.fetchall()]

    if "mail_size" not in columns:
        print("Fuege mail_size Spalte hinzu...")
        cursor.execute("ALTER TABLE mails ADD COLUMN mail_size INTEGER DEFAULT 0")
        conn.commit()
        print("Spalte hinzugefuegt.")

        # body_length als Naehrung setzen wo mail_size noch 0 ist
        cursor.execute("UPDATE mails SET mail_size = body_length WHERE mail_size = 0 AND body_length > 0")
        conn.commit()
        count = cursor.rowcount
        print(f"{count} Mails mit body_length als Naehrungswert aktualisiert.")
    else:
        print("mail_size Spalte existiert bereits.")

    conn.close()
    print("Migration abgeschlossen.")


if __name__ == "__main__":
    migrate()
