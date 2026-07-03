"""The projects.number unique index (migration 0052) makes numbers un-re-usable.

A collision arrives from PostgREST as a 23505 error; _is_duplicate_number is the
pure-logic classifier that turns it into a clean 409 rather than a raw 500. These
tests pin the shapes it must recognize (and the ones it must not swallow)."""

from app.routers.projects import _is_duplicate_number


def test_recognizes_the_number_index_by_name():
    exc = Exception(
        'duplicate key value violates unique constraint "projects_number_unique_idx"'
    )
    assert _is_duplicate_number(exc)


def test_recognizes_23505_mentioning_number():
    exc = Exception("{'code': '23505', 'message': 'number already exists'}")
    assert _is_duplicate_number(exc)


def test_ignores_unrelated_unique_violations():
    # A 23505 on some other table/column must not masquerade as a number clash.
    exc = Exception('duplicate key value violates unique constraint "project_gcs_pkey"')
    assert not _is_duplicate_number(exc)


def test_ignores_non_conflict_errors():
    assert not _is_duplicate_number(Exception("Project not found"))
