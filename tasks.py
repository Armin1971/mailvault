"""MailVault Task Manager – Background-Tasks mit Progress-Reporting."""

import threading
import time
import json
from datetime import datetime


class TaskManager:
    """Verwaltet Background-Tasks mit Live-Progress."""

    def __init__(self):
        self._tasks = {}
        self._lock = threading.Lock()

    def create_task(self, task_id, description=""):
        """Erstellt einen neuen Task."""
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "description": description,
                "status": "running",
                "progress": 0,
                "total": 0,
                "message": "Starte...",
                "detail": "",
                "started_at": datetime.utcnow().isoformat(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
        return self._tasks[task_id]

    def update(self, task_id, progress=None, total=None, message=None, detail=None):
        """Aktualisiert den Progress eines Tasks."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            if progress is not None:
                task["progress"] = progress
            if total is not None:
                task["total"] = total
            if message is not None:
                task["message"] = message
            if detail is not None:
                task["detail"] = detail

    def finish(self, task_id, result=None):
        """Markiert einen Task als abgeschlossen."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task["status"] = "done"
                task["progress"] = task["total"]
                task["finished_at"] = datetime.utcnow().isoformat()
                task["result"] = result
                task["message"] = "Abgeschlossen"

    def fail(self, task_id, error):
        """Markiert einen Task als fehlgeschlagen."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task["status"] = "error"
                task["error"] = str(error)
                task["finished_at"] = datetime.utcnow().isoformat()
                task["message"] = f"Fehler: {error}"

    def get(self, task_id):
        """Gibt den aktuellen Status eines Tasks zurück."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                return dict(task)
            return None

    def get_active(self):
        """Gibt alle aktiven Tasks zurück."""
        with self._lock:
            return [
                dict(t) for t in self._tasks.values()
                if t["status"] == "running"
            ]


# Globale Instanz
task_manager = TaskManager()
