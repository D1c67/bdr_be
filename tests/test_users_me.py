"""Validation tests for the self-service profile update schema."""

import pytest
from pydantic import ValidationError

from app.models.schemas import UpdateMeIn


def test_full_name_is_stripped():
    assert UpdateMeIn(full_name="  Pat Doe  ").full_name == "Pat Doe"

def test_blank_full_name_rejected():
    with pytest.raises(ValidationError):
        UpdateMeIn(full_name="   \n\t ")

def test_empty_full_name_rejected():
    with pytest.raises(ValidationError):
        UpdateMeIn(full_name="")

def test_oversize_full_name_rejected():
    with pytest.raises(ValidationError):
        UpdateMeIn(full_name="x" * 121)
