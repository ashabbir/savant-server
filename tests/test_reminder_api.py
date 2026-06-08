"""TDD tests for Reminders REST API endpoints.

Written BEFORE verifying the implementation (RED → GREEN).
Tests full HTTP round-trip including JSON serialization and status codes.
"""

import sys, os, json
from datetime import datetime, timezone, timedelta
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ────────────────────────────────────────────────────────────────

def _future(hrs=24):
    return (datetime.now(timezone.utc) + timedelta(hours=hrs)).isoformat()


def _past(hrs=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hrs)).isoformat()


def _create(client, title="Test", due_date=None, **kwargs):
    payload = {"title": title, "due_date": due_date or _future(24), **kwargs}
    return client.post("/api/reminders", json=payload)


# ── POST /api/reminders ────────────────────────────────────────────────────

class TestCreateReminder:

    def test_returns_201(self, client):
        resp = _create(client)
        assert resp.status_code == 201

    def test_returns_reminder_id(self, client):
        data = _create(client).get_json()
        assert "reminder_id" in data
        assert data["reminder_id"].startswith("rem-")

    def test_returns_all_required_fields(self, client):
        data = _create(client, title="Buy milk", due_date=_future(10)).get_json()
        required = {
            "reminder_id", "title", "description", "priority", "status",
            "start_date", "due_date", "remind_before_hrs", "created_at", "updated_at",
        }
        for field in required:
            assert field in data, f"Response missing field: {field}"

    def test_default_status_pending(self, client):
        data = _create(client).get_json()
        assert data["status"] == "pending"

    def test_default_priority_medium(self, client):
        data = _create(client).get_json()
        assert data["priority"] == "medium"

    def test_default_remind_before_hrs_1(self, client):
        data = _create(client).get_json()
        assert data["remind_before_hrs"] == 1

    def test_custom_priority(self, client):
        data = _create(client, priority="critical").get_json()
        assert data["priority"] == "critical"

    def test_custom_remind_before_hrs(self, client):
        data = _create(client, remind_before_hrs=3).get_json()
        assert data["remind_before_hrs"] == 3

    def test_stores_description(self, client):
        data = _create(client, description="Don't forget!").get_json()
        assert data["description"] == "Don't forget!"

    def test_missing_title_returns_400(self, client):
        resp = client.post("/api/reminders", json={"due_date": _future(1)})
        assert resp.status_code == 400

    def test_missing_due_date_returns_400(self, client):
        resp = client.post("/api/reminders", json={"title": "Something"})
        assert resp.status_code == 400

    def test_empty_body_returns_400(self, client):
        resp = client.post("/api/reminders", json={})
        assert resp.status_code == 400


# ── GET /api/reminders ────────────────────────────────────────────────────

class TestListReminders:

    def test_empty_list(self, client):
        resp = client.get("/api/reminders")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_created_reminders(self, client):
        _create(client, title="R1")
        _create(client, title="R2")
        data = client.get("/api/reminders").get_json()
        assert len(data) == 2

    def test_filter_by_status_pending(self, client):
        _create(client, title="P1")
        r2 = _create(client, title="P2").get_json()
        client.post(f"/api/reminders/{r2['reminder_id']}/complete")
        data = client.get("/api/reminders?status=pending").get_json()
        assert len(data) == 1
        assert data[0]["title"] == "P1"

    def test_filter_by_status_done(self, client):
        r1 = _create(client, title="D1").get_json()
        _create(client, title="D2")
        client.post(f"/api/reminders/{r1['reminder_id']}/complete")
        data = client.get("/api/reminders?status=done").get_json()
        assert len(data) == 1
        assert data[0]["title"] == "D1"

    def test_sorted_by_due_date_asc(self, client):
        _create(client, title="Far", due_date=_future(72))
        _create(client, title="Near", due_date=_future(12))
        data = client.get("/api/reminders").get_json()
        assert data[0]["title"] == "Near"
        assert data[1]["title"] == "Far"


# ── GET /api/reminders/<id> ────────────────────────────────────────────────

class TestGetReminder:

    def test_returns_correct_reminder(self, client):
        created = _create(client, title="Find me").get_json()
        rid = created["reminder_id"]
        data = client.get(f"/api/reminders/{rid}").get_json()
        assert data["reminder_id"] == rid
        assert data["title"] == "Find me"

    def test_404_for_unknown_id(self, client):
        resp = client.get("/api/reminders/nonexistent-id")
        assert resp.status_code == 404


# ── PUT /api/reminders/<id> ───────────────────────────────────────────────

class TestUpdateReminder:

    def test_update_title(self, client):
        rid = _create(client, title="Old").get_json()["reminder_id"]
        data = client.put(f"/api/reminders/{rid}", json={"title": "New"}).get_json()
        assert data["title"] == "New"

    def test_update_priority(self, client):
        rid = _create(client).get_json()["reminder_id"]
        data = client.put(f"/api/reminders/{rid}", json={"priority": "high"}).get_json()
        assert data["priority"] == "high"

    def test_update_due_date(self, client):
        rid = _create(client).get_json()["reminder_id"]
        new_due = _future(72)
        data = client.put(f"/api/reminders/{rid}", json={"due_date": new_due}).get_json()
        assert data["due_date"] == new_due

    def test_update_404_for_unknown_id(self, client):
        resp = client.put("/api/reminders/nonexistent", json={"title": "X"})
        assert resp.status_code == 404


# ── POST /api/reminders/<id>/complete ────────────────────────────────────

class TestCompleteReminder:

    def test_complete_sets_status_done(self, client):
        rid = _create(client).get_json()["reminder_id"]
        data = client.post(f"/api/reminders/{rid}/complete").get_json()
        assert data["status"] == "done"

    def test_complete_persists(self, client):
        rid = _create(client).get_json()["reminder_id"]
        client.post(f"/api/reminders/{rid}/complete")
        data = client.get(f"/api/reminders/{rid}").get_json()
        assert data["status"] == "done"

    def test_complete_404_unknown(self, client):
        resp = client.post("/api/reminders/nonexistent/complete")
        assert resp.status_code == 404


# ── POST /api/reminders/<id>/dismiss ────────────────────────────────────

class TestDismissReminder:

    def test_dismiss_sets_status_dismissed(self, client):
        rid = _create(client).get_json()["reminder_id"]
        data = client.post(f"/api/reminders/{rid}/dismiss").get_json()
        assert data["status"] == "dismissed"

    def test_dismiss_persists(self, client):
        rid = _create(client).get_json()["reminder_id"]
        client.post(f"/api/reminders/{rid}/dismiss")
        data = client.get(f"/api/reminders/{rid}").get_json()
        assert data["status"] == "dismissed"

    def test_dismiss_404_unknown(self, client):
        resp = client.post("/api/reminders/nonexistent/dismiss")
        assert resp.status_code == 404


# ── DELETE /api/reminders/<id> ────────────────────────────────────────────

class TestDeleteReminder:

    def test_delete_returns_deleted_id(self, client):
        rid = _create(client).get_json()["reminder_id"]
        data = client.delete(f"/api/reminders/{rid}").get_json()
        assert data["deleted"] == rid

    def test_delete_removes_from_list(self, client):
        rid = _create(client).get_json()["reminder_id"]
        client.delete(f"/api/reminders/{rid}")
        resp = client.get("/api/reminders")
        assert resp.get_json() == []

    def test_delete_404_unknown(self, client):
        resp = client.delete("/api/reminders/nonexistent")
        assert resp.status_code == 404


# ── GET /api/reminders/due-today ─────────────────────────────────────────

class TestDueToday:

    def test_returns_only_today(self, client):
        today_noon = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00+00:00")
        _create(client, title="Today", due_date=today_noon)
        _create(client, title="Tomorrow", due_date=_future(48))
        data = client.get("/api/reminders/due-today").get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Today"

    def test_excludes_completed(self, client):
        today_noon = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00+00:00")
        rid = _create(client, title="Done today", due_date=today_noon).get_json()["reminder_id"]
        client.post(f"/api/reminders/{rid}/complete")
        data = client.get("/api/reminders/due-today").get_json()
        assert data == []


# ── GET /api/reminders/due-soon ──────────────────────────────────────────

class TestDueSoon:

    def test_returns_within_window(self, client):
        _create(client, title="Soon", due_date=_future(0.5))
        _create(client, title="Later", due_date=_future(3))
        data = client.get("/api/reminders/due-soon?within_hrs=1").get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Soon"

    def test_custom_window(self, client):
        _create(client, title="In2hrs", due_date=_future(2))
        _create(client, title="In5hrs", due_date=_future(5))
        data = client.get("/api/reminders/due-soon?within_hrs=3").get_json()
        assert len(data) == 1
        assert data[0]["title"] == "In2hrs"

    def test_excludes_dismissed(self, client):
        rid = _create(client, title="Dismissed soon", due_date=_future(0.5)).get_json()["reminder_id"]
        client.post(f"/api/reminders/{rid}/dismiss")
        data = client.get("/api/reminders/due-soon?within_hrs=1").get_json()
        assert data == []


# ── GET /api/reminders/overdue ───────────────────────────────────────────

class TestOverdue:

    def test_returns_past_pending(self, client):
        _create(client, title="Overdue", due_date=_past(2))
        _create(client, title="Future", due_date=_future(2))
        data = client.get("/api/reminders/overdue").get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Overdue"

    def test_excludes_done(self, client):
        rid = _create(client, title="Done past", due_date=_past(1)).get_json()["reminder_id"]
        client.post(f"/api/reminders/{rid}/complete")
        data = client.get("/api/reminders/overdue").get_json()
        assert data == []
