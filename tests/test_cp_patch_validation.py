"""CP PATCH bodies reject explicit JSON null on NOT NULL-backed columns.

These Patch models are dumped with exclude_unset, so a field sent as null would
reach the UPDATE as SET col = NULL and the DB would reject it with a raw 500.
The model validators turn that into a clean 422 at the edge. Null on a nullable
column (an intentional clear) and an omitted field both stay valid.
"""

import pytest
from pydantic import ValidationError

from app.models.schemas import CpClassificationPatch, CpDetailsPatch, CpEmployeePatch


def test_cp_details_patch_rejects_null_on_required():
    for field in ("contract_id", "shift_type", "is_active"):
        with pytest.raises(ValidationError):
            CpDetailsPatch(**{field: None})


def test_cp_details_patch_allows_null_on_nullable_and_omitted():
    # Nullable columns clear cleanly; an empty patch is valid.
    CpDetailsPatch(pwp_number=None, report_type=None, contractor_address_zip=None)
    CpDetailsPatch()
    # A real value on a required field is fine.
    CpDetailsPatch(contract_id="C-123", shift_type="regular", is_active=True)


def test_cp_employee_patch_rejects_null_on_required():
    for field in ("first_name", "last_name", "is_active"):
        with pytest.raises(ValidationError):
            CpEmployeePatch(**{field: None})
    # Nullable identity fields still clear.
    CpEmployeePatch(alt_ee_name=None, ssn_last_four=None, classification_id=None)


def test_cp_classification_patch_rejects_null_on_required():
    for field in ("code", "name", "display_order", "is_field", "is_apprentice"):
        with pytest.raises(ValidationError):
            CpClassificationPatch(**{field: None})
    # Nullable descriptive fields still clear.
    CpClassificationPatch(description=None, apprentice_period=None, percentage_of_journeyman=None)
