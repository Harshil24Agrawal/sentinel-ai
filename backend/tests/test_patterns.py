import re

from app.patterns import Pattern, RegexPatternMiner, mine, pattern_id, template_of


def test_same_template_same_id_for_different_request_ids() -> None:
    a = mine("Database timeout while saving appointment 88214")
    b = mine("Database timeout while saving appointment 90431")
    assert a.id == b.id
    assert a.template == "Database timeout while saving appointment <NUM>"


def test_different_messages_get_different_ids() -> None:
    assert (
        mine("Database timeout while saving appointment 1").id
        != mine("Database timeout while saving claim 1").id
    )


def test_id_is_short_stable_hex() -> None:
    pid = pattern_id("Connection pool exhausted after <NUM> ms")
    assert re.fullmatch(r"[0-9a-f]{8}", pid)
    assert pid == pattern_id("Connection pool exhausted after <NUM> ms")


def test_spec_examples() -> None:
    assert template_of("Connection pool exhausted after 3004 ms") == (
        "Connection pool exhausted after <NUM> ms"
    )
    assert template_of("Token validation failed: signing key a1b2c3d4e5f6 not found") == (
        "Token validation failed: signing key <HEX> not found"
    )


def test_each_token_kind() -> None:
    assert template_of("session 3f2b8c1e-9a4d-4e6f-8b1a-0c2d3e4f5a6b closed") == (
        "session <UUID> closed"
    )
    assert template_of("mail to jane@example.com bounced") == "mail to <EMAIL> bounced"
    assert template_of("peer 10.0.12.7 refused") == "peer <IP> refused"
    assert template_of("took 12.5 s for 3 rows") == "took <NUM> s for <NUM> rows"


def test_uuid_is_not_chopped_up_by_later_steps() -> None:
    assert template_of("id 3f2b8c1e-9a4d-4e6f-8b1a-0c2d3e4f5a6b") == "id <UUID>"


def test_long_digit_only_ids_are_numbers_not_hex() -> None:
    assert template_of("batch 1234567890 done") == "batch <NUM> done"


def test_words_with_digits_stay_stable() -> None:
    assert template_of("db2 replica ok") == "db2 replica ok"


def test_whitespace_is_collapsed() -> None:
    assert template_of("  Query   88214 \t aborted  ") == "Query <NUM> aborted"


def test_redacted_marker_stays_literal() -> None:
    a = template_of("saved member_id=[REDACTED] for 55555")
    b = template_of("saved member_id=[REDACTED] for 66666")
    assert a == b == "saved member_id=[REDACTED] for <NUM>"


def test_miner_interface() -> None:
    p = RegexPatternMiner().mine("Query 5 failed")
    assert p == Pattern(id=pattern_id("Query <NUM> failed"), template="Query <NUM> failed")
