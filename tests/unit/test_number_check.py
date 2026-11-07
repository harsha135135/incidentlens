"""§14 arithmetic check: numbers in claims are looked up in the cited evidence (decisions D50)."""

from incidentlens.reports import check_numbers, numbers_in, number_supported


def test_numbers_ignore_handles_and_clock_times():
    assert numbers_in("E2 shows 1546 failures between 11:29:04 and 11:30:40, p95 0.24 s") == ["1546", "0.24"]
    assert numbers_in("no 5xx or network errors") == []


def test_stated_precision_is_respected():
    assert number_supported("0.24", "p95 0.2414 s")
    assert number_supported("3.2", "arrivals 3.199/s")
    assert not number_supported("3.5", "arrivals 3.199/s")
    assert number_supported("1546", "1546 matching records")


def test_unverified_numbers_are_flagged_on_the_finding():
    findings = [{"claim": "1546 attempts failed over about 100 s", "evidence_handles": ["E3"]}]
    result = check_numbers(findings, {"E3": "filter any: 1879 matching records; network_error 1546"})
    assert findings[0]["unverified_numbers"] == ["100"]
    assert result == {"numbers_checked": 2, "numbers_not_found_in_cited_evidence": 1}


def test_a_percentage_matches_the_ratio_it_restates():
    findings = [{"claim": "45 of 557 requests returned 500 (8.1%), not 9 %", "evidence_handles": ["E1"]}]
    result = check_numbers(findings, {"E1": "557 requests; status 500×45; server_error_ratio 0.0808"})
    assert findings[0]["unverified_numbers"] == ["9"]
    assert result == {"numbers_checked": 5, "numbers_not_found_in_cited_evidence": 1}
    assert not number_supported("8.1", "server_error_ratio 0.0808")  # only a stated percentage is rescaled
