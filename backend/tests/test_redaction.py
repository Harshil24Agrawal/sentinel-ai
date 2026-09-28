import json
import random

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


# ---- the key-presence fast path must not weaken redaction ------------------------------
# `redact` skips the three key-based regexes when no PHI key appears anywhere in the text.
# These tests pin down that the skip is case-insensitive, still sees JSON keys, and never
# changes the outcome compared with running every regex unconditionally.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SSN=123-45-6789", f"SSN={REDACTED}"),
        ("Ssn=123-45-6789", f"Ssn={REDACTED}"),
        ('Patient_Name="Jane Doe"', f"Patient_Name={REDACTED}"),
        ("PATIENT_NAME=Jane", f"PATIENT_NAME={REDACTED}"),
        ("MEMBER_ID=XYZ-1", f"MEMBER_ID={REDACTED}"),  # not a MBR pattern: the KEY must catch it
        ("Member_Id=A1", f"Member_Id={REDACTED}"),
        ("EMAIL=jane@example.com", f"EMAIL={REDACTED}"),
        ("eMail=jane@example.com", f"eMail={REDACTED}"),
        ("Phone=555-123-4567", f"Phone={REDACTED}"),
        ("PHONE=5551234567", f"PHONE={REDACTED}"),  # bare digits: only the KEY catches it
        ("DoB=1980-01-02", f"DoB={REDACTED}"),
        ("MRN=A1B2C3", f"MRN={REDACTED}"),
        ("Address='12 Elm St'", f"Address={REDACTED}"),
        ("ADDRESS=12", f"ADDRESS={REDACTED}"),
        ("NaMe=Jane", f"NaMe={REDACTED}"),
    ],
)
def test_mixed_case_keys_are_redacted(redactor: Redactor, text: str, expected: str) -> None:
    out, n = redactor.redact(text)
    assert out == expected
    assert n >= 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"Member_ID":"MBR1234567"}', f'{{"Member_ID":"{REDACTED}"}}'),
        ('{"Member_ID":"ZZ-9"}', f'{{"Member_ID":"{REDACTED}"}}'),  # only the key can catch this
        ('{"SSN":"123456789"}', f'{{"SSN":"{REDACTED}"}}'),
        ('{"Patient_Name": "Jane Doe"}', f'{{"Patient_Name": "{REDACTED}"}}'),
        ('{"EMAIL": "jane@example.com"}', f'{{"EMAIL": "{REDACTED}"}}'),
        ('{"pHoNe": 5551234567}', f'{{"pHoNe": "{REDACTED}"}}'),  # numeric value
        ('{"MRN": 4412, "level": "INFO"}', f'{{"MRN": "{REDACTED}", "level": "INFO"}}'),
        ('{"DOB":"1980-01-02","service":"a"}', f'{{"DOB":"{REDACTED}","service":"a"}}'),
    ],
)
def test_mixed_case_json_keys_are_redacted(redactor: Redactor, text: str, expected: str) -> None:
    out, n = redactor.redact(text)
    assert out == expected
    assert n >= 1
    json.loads(out)  # and the result is still valid JSON


def test_pattern_based_redaction_needs_no_key_at_all(redactor: Redactor) -> None:
    out, n = redactor.redact("no key here: 123-45-6789 jane@example.com MBR1234567 555-123-4567")
    assert out == f"no key here: {REDACTED} {REDACTED} {REDACTED} {REDACTED}"
    assert n == 4


def test_a_key_hiding_inside_a_longer_word_is_not_over_redacted(redactor: Redactor) -> None:
    text = "Hostname=db1 filename=report.txt USERNAME=svc"
    assert redactor.redact(text) == (text, 0)  # the fast path lets these through to the regexes


def test_key_in_the_middle_of_a_long_line_is_still_found(redactor: Redactor) -> None:
    line = "2026-09-28T10:00:01Z INFO svc latency_ms=12 " + "x" * 500 + " SSN=123-45-6789 tail"
    out, n = redactor.redact(line)
    assert f"SSN={REDACTED} tail" in out and "123-45-6789" not in out and n == 1


def test_custom_keys_are_case_insensitive_too() -> None:
    r = Redactor(phi_keys=["badge_id"])
    assert r.redact("BADGE_ID=77 Badge_Id=88") == (f"BADGE_ID={REDACTED} Badge_Id={REDACTED}", 2)
    assert r.redact("ssn=1") == ("ssn=1", 0)  # only the configured keys


def _reference_redact(r: Redactor, text: str) -> tuple[str, int]:
    """The redactor WITHOUT the fast path: every regex, unconditionally."""
    count = 0
    text, n = r._kv_quoted.subn(lambda m: m["pre"] + REDACTED, text)
    count += n
    text, n = r._kv_bare.subn(lambda m: m["pre"] + REDACTED, text)
    count += n
    text, n = r._json.subn(lambda m: m["pre"] + f'"{REDACTED}"', text)
    count += n
    text, n = r._pattern_based.subn(REDACTED, text)
    return text, count + n


def _mutate_case(text: str, rng: random.Random) -> str:
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in text)


def test_fast_path_gives_exactly_the_same_result_as_running_every_regex(
    redactor: Redactor,
) -> None:
    rng = random.Random(11)
    keys = ["ssn", "member_id", "patient_name", "name", "dob", "email", "phone", "address", "mrn"]
    values = [
        "123-45-6789",
        "MBR1234567",
        '"Jane Doe"',
        "jane@example.com",
        "5551234567",
        "x",
        "42",
    ]
    shapes = [
        "{k}={v}",
        "prefix {k}={v} suffix",
        '{{"{k}":"{v}"}}',
        '{{"{k}": {v}}}',
        "2026-09-28T10:00:01Z INFO svc latency_ms=9 msg {k}={v} more",
        "{k} is {v}",  # a key with no '=' or ':' next to it
        "no phi words at all, request 88214 HTTP 503",
        "{v}",
    ]
    checked = 0
    for _ in range(1500):
        text = rng.choice(shapes).format(k=rng.choice(keys), v=rng.choice(values))
        text = _mutate_case(text, rng) if rng.random() < 0.7 else text
        assert redactor.redact(text, track=False) == _reference_redact(redactor, text), text
        checked += 1
    assert checked == 1500
