"""The recruitment agent register and its performance view (FR-102 to FR-107).

The properties worth pinning down are the ones a compliance visit would ask about: that the figures
are a view over the applications themselves rather than a second copy, that a suspended agent stays
on the register even though it can no longer be attributed new applications, and that a refusal rate
is withheld below the small-denominator floor instead of being reported from a handful of decisions.
"""

from __future__ import annotations

import pytest

from tests.fixtures import (
    API,
    assign_cas,
    record_visa,
    run_to_precas,
)


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


def by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def test_register_lists_every_agent_including_suspended(client, reference, team):
    """FR-102: a suspended agent stays visible. Its history does not disappear with its status."""
    res = client.get(f"{API}/agents", headers=team["compliance"].headers)
    assert res.status_code == 200
    rows = by_name(res.json())
    assert "Test Agent Ltd" in rows
    assert rows["Suspended Agent"]["status"] == "suspended"


def test_country_is_resolved_to_a_name(client, reference, team):
    """The register is read by people, not by systems: 'IN' is shown as India."""
    rows = by_name(client.get(f"{API}/agents", headers=team["admissions"].headers).json())
    assert rows["Test Agent Ltd"]["country"] == "India"


def test_finance_and_applicants_cannot_read_the_register(client, reference, team, applicant):
    """LLD 6: agents belong to recruitment and compliance. Finance has no part in them."""
    assert client.get(f"{API}/agents", headers=team["finance"].headers).status_code == 403
    assert client.get(f"{API}/agents", headers=applicant.headers).status_code == 403


def test_counts_follow_the_applications_attributed_to_the_agent(
    client, applicant, draft, reference, team
):
    """FR-104: one application through to CAS moves that agent's counts, and nobody else's."""
    before = by_name(client.get(f"{API}/agents", headers=team["compliance"].headers).json())
    assert before["Test Agent Ltd"]["applications"] == 0

    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], agent_id=reference["agent_id"])  # fmt: skip
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201

    after = by_name(client.get(f"{API}/agents", headers=team["compliance"].headers).json())
    row = after["Test Agent Ltd"]
    assert row["applications"] == 1
    assert row["offers"] == 1
    assert row["cas"] == 1
    assert row["enrolled"] == 0  # nobody has enrolled yet
    assert after["Suspended Agent"]["applications"] == 0


def test_refusal_rate_is_withheld_below_the_small_denominator(
    client, applicant, draft, reference, team
):
    """LLD 8.3: one refusal out of one decision is not a 100% refusal rate, it is one refusal."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], agent_id=reference["agent_id"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)
    record_visa(client, team["compliance"], draft["id"], outcome="refused",
                category="financial_requirement")  # fmt: skip

    row = by_name(client.get(f"{API}/agents", headers=team["compliance"].headers).json())["Test Agent Ltd"]
    assert row["visa_decisions"] == 1
    assert row["refused"] == 1
    assert row["refusal_rate_pct"] is None
    assert row["refusal_rate_over_threshold"] is False
