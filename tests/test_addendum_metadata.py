from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import files


class FileStore:
    def __init__(self, row):
        self.row = row
        self.race = False

    def table(self, name):
        assert name == "project_files"
        self.filters = {}
        self.updates = None
        return self

    def select(self, columns):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def is_(self, key, value):
        assert value == "null"
        self.filters[key] = None
        return self

    def limit(self, count):
        return self

    def update(self, values):
        self.updates = values
        if self.race:
            self.row["sent_to_estimators_at"] = "2026-10-26T20:00:00Z"
        return self

    def execute(self):
        if not self.row or any(self.row.get(k) != v for k, v in self.filters.items()):
            return SimpleNamespace(data=[])
        if self.updates:
            self.row.update(self.updates)
        return SimpleNamespace(data=[self.row.copy()])


@pytest.fixture
def store(monkeypatch):
    store = FileStore({
        "id": "f1", "project_id": "p1", "category": "addendum",
        "addendum_number": "1", "addendum_issued_on": None,
        "sent_to_estimators_at": None,
    })
    monkeypatch.setattr(files, "get_supabase", lambda: store)
    monkeypatch.setattr(files, "audit", lambda *args: None)
    return store


def edit(project="p1", role=Role.ESTIMATING_ADMIN):
    return files.update_addendum_metadata(
        project, "f1",
        files.AddendumMetadataIn(addendum_number=" 1 ", addendum_issued_on=date.today()),
        CurrentUser(id="u1", email="admin@example.com", role=role, is_active=True),
    )


def test_complete_imported_addendum_and_correct_date(store):
    assert edit()["addendum_issued_on"] == date.today().isoformat()
    assert store.row["addendum_number"] == "1"
    yesterday = date.today() - timedelta(days=1)
    result = files.update_addendum_metadata(
        "p1", "f1", files.AddendumMetadataIn(
            addendum_number="1", addendum_issued_on=yesterday,
        ), CurrentUser(id="u1", email="admin@example.com",
                       role=Role.ESTIMATING_ADMIN, is_active=True),
    )
    assert result["addendum_issued_on"] == yesterday.isoformat()


@pytest.mark.parametrize("role", [Role.ESTIMATOR, Role.ACCOUNTANT])
def test_read_only_users_cannot_edit(store, role):
    with pytest.raises(HTTPException) as exc:
        edit(role=role)
    assert exc.value.status_code == 403
    assert store.row["addendum_issued_on"] is None


def test_other_project_is_not_modified(store):
    with pytest.raises(HTTPException) as exc:
        edit(project="other-project")
    assert exc.value.status_code == 404
    assert store.row["addendum_issued_on"] is None


@pytest.mark.parametrize("race", [False, True])
def test_sent_addenda_are_immutable_including_concurrent_send(store, race):
    store.race = race
    if not race:
        store.row["sent_to_estimators_at"] = "2026-10-26T20:00:00Z"
    with pytest.raises(HTTPException) as exc:
        edit()
    assert exc.value.status_code == 409
    assert store.row["addendum_issued_on"] is None


def test_other_categories_cannot_be_changed(store):
    store.row["category"] = "drawing"
    with pytest.raises(HTTPException) as exc:
        edit()
    assert exc.value.status_code == 400


@pytest.mark.parametrize("value", ["", "not-a-date", "2026-02-30", None])
def test_invalid_dates_are_rejected(value):
    with pytest.raises(ValidationError):
        files.AddendumMetadataIn(addendum_number="1", addendum_issued_on=value)


def test_future_date_is_rejected():
    with pytest.raises(ValidationError):
        files.AddendumMetadataIn(
            addendum_number="1", addendum_issued_on=date.today() + timedelta(days=3),
        )
