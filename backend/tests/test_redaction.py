import json

import pytest

from app.redaction import REDACTED, Redactor


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


@pytest.mark.parametrize(
    "text",
    [
        "user ssn 123-45-6789 rejected",
        "contact jane.doe@example.com failed",
        "call 555-123-4567 back",
        "call (555) 123-4567 back",
        "call +1 555.123.4567 back",
        "claim for MBR1234567 denied",
        "claim for MBR-1234567 denied",
    ],
)
def test_pattern_based_phi_is_redacted(redactor: Redactor, text: str) -> None:
    out, n = redactor.redact(text)
    assert REDACTED in out
    assert n == 1
    for leaked in ("123-45-6789", "example.com", "123-4567", "MBR"):
        assert leaked not in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("saved member_id=MBR1234567 ok", f"saved member_id={REDACTED} ok"),
        ("saved ssn=123-45-6789 ok", f"saved ssn={REDACTED} ok"),
        ("saved email=jane@example.com ok", f"saved email={REDACTED} ok"),
        ('saved patient_name="Jane Doe" ok', f"saved patient_name={REDACTED} ok"),
        ("saved dob=1980-01-02 mrn=A1B2C3 ok", f"saved dob={REDACTED} mrn={REDACTED} ok"),
        ("saved NAME=Jane ok", f"saved NAME={REDACTED} ok"),
        ("saved phone=5551234567 ok", f"saved phone={REDACTED} ok"),
        ("saved address='12 Elm St' ok", f"saved address={REDACTED} ok"),
    ],
)
def test_key_based_values_are_redacted(redactor: Redactor, text: str, expected: str) -> None:
    out, n = redactor.redact(text)
    assert out == expected
    assert n >= 1


def test_json_keys_are_redacted(redactor: Redactor) -> None:
    out, n = redactor.redact('{"service":"a","ssn":"123456789","mrn": 4412,"email":"x@y.org"}')
    assert n == 3
    assert json.loads(out) == {"service": "a", "ssn": REDACTED, "mrn": REDACTED, "email": REDACTED}


def test_bare_key_value_inside_json_string_keeps_json_valid(redactor: Redactor) -> None:
    out, _ = redactor.redact('{"msg":"login email=jane@example.com","service":"auth"}')
    assert json.loads(out)["msg"] == f"login email={REDACTED}"


@pytest.mark.parametrize(
    "text",
    [
        "Appointment request 88214 completed",
        "Database timeout while saving appointment 88214",
        "latency_ms=120 Query 90431 completed",
        "Request 51234 failed with HTTP 503",
        "2026-09-28T10:00:01.123Z INFO database Checkpoint completed in 312 ms",
        "Token validation failed: signing key a1b2c3d4e5f6 not found",
        "claim batch 7788123456 accepted",  # bare 10-digit ID is not a phone number
        "servicename=api hostname=db1",  # only whole-word keys match
    ],
)
def test_ordinary_text_is_untouched(redactor: Redactor, text: str) -> None:
    assert redactor.redact(text) == (text, 0)


def test_counter_accumulates_and_track_false_does_not_count(redactor: Redactor) -> None:
    redactor.redact("ssn=123-45-6789 email=a@b.com")
    assert redactor.total == 2
    redactor.redact("ssn=123-45-6789", track=False)
    assert redactor.total == 2
    redactor.reset()
    assert redactor.total == 0


def test_redaction_is_idempotent(redactor: Redactor) -> None:
    once, _ = redactor.redact('ssn=123-45-6789 patient_name="Jane Doe" {"email":"a@b.com"}')
    twice, n = redactor.redact(once)
    assert twice == once
    assert n == 0


def test_custom_keys_and_member_id_pattern() -> None:
    r = Redactor(phi_keys=["badge"], member_id_pattern=r"\bZZ\d{4}\b")
    out, n = r.redact("badge=77 id ZZ1234 ssn=1")
    assert out == f"badge={REDACTED} id {REDACTED} ssn=1"
    assert n == 2
