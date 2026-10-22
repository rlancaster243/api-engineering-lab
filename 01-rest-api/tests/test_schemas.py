import unicodedata
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ItemCreate,
    ItemPatch,
    ItemResponse,
    normalize_content_id,
)

HTTPS = "https://example.org/a"


# --- content_id -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("article-042", "article-042"),
        ("  a1\t\n", "a1"),
        ("\x0bx\x0c", "x"),
        ("Mixed-Case", "Mixed-Case"),  # comparison is case-sensitive: no folding
        (" x", " x"),  # NBSP is outside the trim set the database also uses
    ],
)
def test_content_id_trims_only_the_database_trim_set(raw: str, expected: str) -> None:
    assert normalize_content_id(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "\t", "\x0b", " \t\n\r\f\v "])
def test_content_id_blank_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        normalize_content_id(raw)


def test_content_id_is_nfc_normalized() -> None:
    decomposed = "café"
    assert normalize_content_id(decomposed) == unicodedata.normalize("NFC", decomposed)
    assert normalize_content_id(decomposed) == normalize_content_id("café")


def test_content_id_length_is_measured_after_normalization() -> None:
    assert normalize_content_id("a" * 255) == "a" * 255
    with pytest.raises(ValueError, match="255"):
        normalize_content_id("a" * 256)
    # 400 code points before NFC, 200 after.
    assert len(normalize_content_id("é" * 200)) == 200


# --- create -----------------------------------------------------------------------------


def test_create_accepts_minimal_and_full_bodies() -> None:
    assert ItemCreate(content_id="a").model_dump() == {
        "content_id": "a",
        "title": None,
        "url": None,
        "notes": None,
    }
    full = ItemCreate(content_id=" a ", title="T", url=HTTPS, notes="n")
    assert full.content_id == "a"


@pytest.mark.parametrize("field", ["user_id", "id", "created_at", "unknown"])
def test_create_rejects_unknown_fields(field: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ItemCreate.model_validate({"content_id": "a", field: "x"})
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


def test_create_requires_content_id() -> None:
    with pytest.raises(ValidationError):
        ItemCreate.model_validate({"title": "t"})


@pytest.mark.parametrize(
    ("title", "valid"),
    [
        (None, True),
        ("T", True),
        (" padded ", True),  # not trimmed on store; only blank is rejected
        ("x" * 200, True),
        ("x" * 201, False),
        ("", False),
        ("   ", False),
        ("\t\n", False),
    ],
)
def test_title_rules(title: str | None, valid: bool) -> None:
    if valid:
        assert ItemCreate(content_id="a", title=title).title == title
    else:
        with pytest.raises(ValidationError):
            ItemCreate(content_id="a", title=title)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/a",
        "http://example.org",
        "HTTP://EXAMPLE.ORG/x",
        "https://h/" + "a" * 2038,
    ],
)
def test_url_accepts_http_and_https(url: str) -> None:
    assert len(url) <= 2048
    assert ItemCreate(content_id="a", url=url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://example.org/a",
        "https://",
        "//example.org/a",
        "example.org/a",
        "https://example.org/a b",
        "https://example.org/a\n",
        "https://example.org/\x00",
        "https://h/" + "a" * 2039,  # 2049 characters
    ],
)
def test_url_rejects_non_http_malformed_and_oversized(url: str) -> None:
    with pytest.raises(ValidationError):
        ItemCreate(content_id="a", url=url)


@pytest.mark.parametrize(("notes", "valid"), [("n" * 2000, True), ("n" * 2001, False)])
def test_notes_length(notes: str, valid: bool) -> None:
    if valid:
        assert ItemCreate(content_id="a", notes=notes).notes == notes
    else:
        with pytest.raises(ValidationError):
            ItemCreate(content_id="a", notes=notes)


# --- patch ------------------------------------------------------------------------------


def test_patch_rejects_an_empty_body() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        ItemPatch.model_validate({})


def test_patch_distinguishes_omitted_from_explicit_null() -> None:
    assert ItemPatch.model_validate({"title": "t"}).changes() == {"title": "t"}
    assert ItemPatch.model_validate({"notes": None}).changes() == {"notes": None}
    assert ItemPatch.model_validate({"title": None, "url": HTTPS}).changes() == {
        "title": None,
        "url": HTTPS,
    }


def test_patch_with_unchanged_looking_values_is_still_valid_input() -> None:
    # Whether it mutates anything is decided against stored state, not by the schema.
    assert ItemPatch.model_validate({"title": "same"}).changes() == {"title": "same"}


@pytest.mark.parametrize("field", ["content_id", "user_id", "id"])
def test_patch_forbids_immutable_fields(field: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ItemPatch.model_validate({"title": "t", field: "x"})
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


def test_patch_applies_the_same_value_rules_as_create() -> None:
    for body in ({"title": "   "}, {"url": "ftp://x"}, {"notes": "n" * 2001}):
        with pytest.raises(ValidationError):
            ItemPatch.model_validate(body)


# --- response ---------------------------------------------------------------------------


def _response(created: datetime, updated: datetime | None = None) -> ItemResponse:
    return ItemResponse(
        id="c83cae64-4195-475b-9a05-96be0e52834a",
        content_id="a",
        title=None,
        url=None,
        notes=None,
        created_at=created,
        updated_at=updated or created,
    )


def test_response_has_no_user_id() -> None:
    assert "user_id" not in ItemResponse.model_fields


def test_response_timestamps_are_utc_z_and_keep_microseconds() -> None:
    whole = _response(datetime(2026, 10, 16, 14, 0, tzinfo=UTC)).model_dump(mode="json")
    assert whole["created_at"] == "2026-10-16T14:00:00Z"
    micro = _response(datetime(2026, 10, 16, 14, 0, 0, 123456, tzinfo=UTC)).model_dump(mode="json")
    assert micro["created_at"] == "2026-10-16T14:00:00.123456Z"


def test_response_converts_other_offsets_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    dumped = _response(datetime(2026, 10, 16, 16, 0, tzinfo=plus_two)).model_dump(mode="json")
    assert dumped["created_at"] == "2026-10-16T14:00:00Z"


def test_response_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        _response(datetime(2026, 10, 16, 14, 0))


def test_pagination_limit_constants_match_the_spec() -> None:
    assert (DEFAULT_LIMIT, MAX_LIMIT) == (25, 100)
