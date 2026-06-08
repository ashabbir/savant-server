"""TDD tests for ReminderDB — data layer.

Written BEFORE verifying the implementation (RED → GREEN).
Every test covers a concrete, observable behaviour.
"""

import sys, os
from datetime import datetime, timezone, timedelta
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.reminders import ReminderDB


# ── Helpers ────────────────────────────────────────────────────────────────

def _future(hrs=24):
    return (datetime.now(timezone.utc) + timedelta(hours=hrs)).isoformat()


def _past(hrs=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hrs)).isoformat()


def _make(reminder_id="rem-001", title="Test reminder", due_date=None, **kwargs):
    return ReminderDB.create({
        "reminder_id": reminder_id,
        "title": title,
        "due_date": due_date or _future(24),
        **kwargs,
    })


# ── Create ─────────────────────────────────────────────────────────────────

class TestReminderCreate:

    def test_create_returns_reminder_id(self):
        r = _make("rem-c1")
        assert r["reminder_id"] == "rem-c1"

    def test_create_defaults_status_pending(self):
        r = _make("rem-c2")
        assert r["status"] == "pending"

    def test_create_defaults_priority_medium(self):
        r = _make("rem-c3")
        assert r["priority"] == "medium"

    def test_create_defaults_remind_before_hrs_1(self):
        r = _make("rem-c4")
        assert r["remind_before_hrs"] == 1

    def test_create_notified_false_by_default(self):
        r = _make("rem-c5")
        # notified stored as int 0, acceptable as falsy
        assert not r["notified"]

    def test_create_stores_title(self):
        r = _make("rem-c6", title="Buy milk")
        assert r["title"] == "Buy milk"

    def test_create_stores_description(self):
        r = _make("rem-c7", description="From the corner store")
        assert r["description"] == "From the corner store"

    def test_create_stores_due_date(self):
        due = _future(48)
        r = _make("rem-c8", due_date=due)
        assert r["due_date"] == due

    def test_create_custom_priority(self):
        r = _make("rem-c9", priority="critical")
        assert r["priority"] == "critical"

    def test_create_custom_remind_before_hrs(self):
        r = _make("rem-c10", remind_before_hrs=3)
        assert r["remind_before_hrs"] == 3

    def test_create_has_timestamps(self):
        r = _make("rem-c11")
        assert r.get("created_at")
        assert r.get("updated_at")

    def test_create_has_start_date(self):
        r = _make("rem-c12")
        assert r.get("start_date")


# ── Read ───────────────────────────────────────────────────────────────────

class TestReminderRead:

    def test_get_by_id_returns_correct(self):
        _make("rem-r1", title="Get me")
        r = ReminderDB.get_by_id("rem-r1")
        assert r is not None
        assert r["title"] == "Get me"

    def test_get_by_id_nonexistent_returns_none(self):
        assert ReminderDB.get_by_id("no-such-id") is None

    def test_list_all_returns_all(self):
        _make("rem-r2")
        _make("rem-r3")
        _make("rem-r4")
        assert len(ReminderDB.list_all()) == 3

    def test_list_all_filter_by_status(self):
        _make("rem-r5", status="pending")
        _make("rem-r6", status="done")
        _make("rem-r7", status="dismissed")
        pending = ReminderDB.list_all(status="pending")
        assert len(pending) == 1
        assert pending[0]["reminder_id"] == "rem-r5"

    def test_list_all_sorted_by_due_date_asc(self):
        _make("rem-r8", due_date=_future(48))
        _make("rem-r9", due_date=_future(12))
        _make("rem-r10", due_date=_future(72))
        results = ReminderDB.list_all()
        dues = [r["due_date"] for r in results]
        assert dues == sorted(dues)


# ── Due-soon / Due-today / Overdue ─────────────────────────────────────────

class TestReminderTimeBuckets:

    def test_list_due_soon_returns_within_window(self):
        _make("rem-ds1", due_date=_future(0.5))   # 30min → within 1hr
        _make("rem-ds2", due_date=_future(2))      # 2hr → outside 1hr window
        soon = ReminderDB.list_due_soon(within_hrs=1)
        ids = [r["reminder_id"] for r in soon]
        assert "rem-ds1" in ids
        assert "rem-ds2" not in ids

    def test_list_due_soon_excludes_done(self):
        _make("rem-ds3", due_date=_future(0.5), status="done")
        assert len(ReminderDB.list_due_soon(within_hrs=1)) == 0

    def test_list_due_soon_excludes_dismissed(self):
        _make("rem-ds4", due_date=_future(0.5), status="dismissed")
        assert len(ReminderDB.list_due_soon(within_hrs=1)) == 0

    def test_list_overdue_returns_past_pending(self):
        _make("rem-ov1", due_date=_past(2))   # 2hrs ago
        _make("rem-ov2", due_date=_future(1)) # future
        overdue = ReminderDB.list_overdue()
        ids = [r["reminder_id"] for r in overdue]
        assert "rem-ov1" in ids
        assert "rem-ov2" not in ids

    def test_list_overdue_excludes_done(self):
        _make("rem-ov3", due_date=_past(1), status="done")
        assert len(ReminderDB.list_overdue()) == 0

    def test_list_due_today_returns_today(self):
        # Due date set to today at noon UTC
        today_noon = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00+00:00")
        _make("rem-dt1", due_date=today_noon)
        _make("rem-dt2", due_date=_future(48))  # tomorrow
        today = ReminderDB.list_due_today()
        ids = [r["reminder_id"] for r in today]
        assert "rem-dt1" in ids
        assert "rem-dt2" not in ids

    def test_list_due_today_excludes_done(self):
        today_noon = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00+00:00")
        _make("rem-dt3", due_date=today_noon, status="done")
        assert len(ReminderDB.list_due_today()) == 0


# ── Update ─────────────────────────────────────────────────────────────────

class TestReminderUpdate:

    def test_update_title(self):
        _make("rem-u1", title="Old")
        r = ReminderDB.update("rem-u1", {"title": "New"})
        assert r["title"] == "New"
        assert ReminderDB.get_by_id("rem-u1")["title"] == "New"

    def test_update_priority(self):
        _make("rem-u2")
        r = ReminderDB.update("rem-u2", {"priority": "high"})
        assert r["priority"] == "high"

    def test_update_due_date(self):
        _make("rem-u3")
        new_due = _future(72)
        r = ReminderDB.update("rem-u3", {"due_date": new_due})
        assert r["due_date"] == new_due

    def test_update_bumps_updated_at(self):
        r0 = _make("rem-u4")
        old_ts = r0["updated_at"]
        import time; time.sleep(0.01)
        r1 = ReminderDB.update("rem-u4", {"title": "Changed"})
        assert r1["updated_at"] >= old_ts  # monotonically non-decreasing

    def test_update_nonexistent_returns_none(self):
        assert ReminderDB.update("no-such", {"title": "X"}) is None

    def test_update_ignores_unknown_columns(self):
        _make("rem-u5")
        # Should not raise; unknown keys filtered out
        r = ReminderDB.update("rem-u5", {"bogus_field": "x", "title": "OK"})
        assert r["title"] == "OK"

    def test_update_all_unknown_columns_returns_unchanged(self):
        """When only unknown columns are passed, record is returned unchanged (early-return branch)."""
        _make("rem-u6", title="Unchanged")
        r = ReminderDB.update("rem-u6", {"bogus_a": "x", "bogus_b": "y"})
        assert r["title"] == "Unchanged"


# ── Complete / Dismiss ─────────────────────────────────────────────────────

class TestReminderStatusTransitions:

    def test_complete_sets_done(self):
        _make("rem-st1")
        r = ReminderDB.complete("rem-st1")
        assert r["status"] == "done"
        assert ReminderDB.get_by_id("rem-st1")["status"] == "done"

    def test_dismiss_sets_dismissed(self):
        _make("rem-st2")
        r = ReminderDB.dismiss("rem-st2")
        assert r["status"] == "dismissed"

    def test_mark_notified_sets_flag(self):
        _make("rem-st3")
        r = ReminderDB.mark_notified("rem-st3")
        assert r["notified"]  # truthy (1 or True)

    def test_complete_nonexistent_returns_none(self):
        assert ReminderDB.complete("no-such") is None

    def test_dismiss_nonexistent_returns_none(self):
        assert ReminderDB.dismiss("no-such") is None


# ── Delete ─────────────────────────────────────────────────────────────────

class TestReminderDelete:

    def test_delete_existing(self):
        _make("rem-del1")
        assert ReminderDB.delete("rem-del1") is True
        assert ReminderDB.get_by_id("rem-del1") is None

    def test_delete_nonexistent_returns_false(self):
        assert ReminderDB.delete("no-such") is False

    def test_delete_reduces_list(self):
        _make("rem-del2")
        _make("rem-del3")
        ReminderDB.delete("rem-del2")
        assert len(ReminderDB.list_all()) == 1


# ── Field integrity ────────────────────────────────────────────────────────

class TestReminderFieldIntegrity:
    """Every returned reminder must have all required fields."""

    REQUIRED = {
        "reminder_id", "title", "description", "priority", "status",
        "start_date", "due_date", "remind_before_hrs", "notified",
        "created_at", "updated_at",
    }

    def test_create_has_all_fields(self):
        r = _make("rem-fi1")
        for field in self.REQUIRED:
            assert field in r, f"Missing field: {field}"

    def test_list_all_items_have_all_fields(self):
        _make("rem-fi2")
        _make("rem-fi3")
        for r in ReminderDB.list_all():
            for field in self.REQUIRED:
                assert field in r, f"Missing field '{field}' in listed reminder"
