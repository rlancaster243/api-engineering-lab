"""Fails when the error table in the merged spec and the code disagree.

The spec (docs/PROJECT_1_ENGINEERING_FIDELITY.md section 4.5) is the contract; the code
must follow it. Changing one without the other is a contract change that needs review.
"""

import re
from pathlib import Path

from app.errors import ERROR_SPECS

SPEC = Path(__file__).resolve().parents[2] / "docs" / "PROJECT_1_ENGINEERING_FIDELITY.md"


def _spec_error_table() -> dict[str, int]:
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### 4.5 Errors and statuses", 1)[1].split("\n### ", 1)[0]
    rows = re.findall(r"^\|.*\| (\d{3}) \| `([A-Z_]+)` \|$", section, flags=re.MULTILINE)
    return {code: int(status) for status, code in rows}


def test_spec_table_was_found_and_is_not_empty() -> None:
    assert len(_spec_error_table()) >= 10


def test_error_codes_and_statuses_match_the_spec() -> None:
    in_code = {code.value: spec.status for code, spec in ERROR_SPECS.items()}
    assert in_code == _spec_error_table()
