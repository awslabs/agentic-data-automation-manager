#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The demo dataset agrees with itself, and the checker notices when it does not.

The previous data was generated table by table and nothing agreed: running
balances were NULL on all 10,098 transactions, positions matched net trades for 1
of 1,698 holdings, 274 of 300 contacts were logged by someone else's RM, and goal
progress was a number unrelated to any account. Each mutation below reintroduces
one of those defects and asserts check() reports it.
"""

import copy
from decimal import Decimal

import pytest

from demo_dataset import generate as g


@pytest.fixture(scope="module")
def data():
    return g.generate()


def test_generated_data_satisfies_every_invariant(data):
    assert g.check(data) == []


def test_generation_is_deterministic(data):
    assert g.generate() == data


def _mutated(data, fn):
    d = copy.deepcopy(data)
    fn(d)
    return g.check(d)


def test_detects_a_wrong_running_balance(data):
    def corrupt(d):
        d["TRANSACTIONS"][5]["RUNNING_BALANCE"] += Decimal("1.00")
    assert any("running balance does not follow" in f for f in _mutated(data, corrupt))


def test_detects_a_missing_running_balance(data):
    def corrupt(d):
        d["TRANSACTIONS"][5]["RUNNING_BALANCE"] = None
    assert any("running balance is missing" in f for f in _mutated(data, corrupt))


def test_detects_balance_disagreeing_with_transactions(data):
    def corrupt(d):
        d["ACCOUNTS"][0]["BALANCE"] += Decimal("100")
    assert any("balance != latest" in f for f in _mutated(data, corrupt))


def test_detects_position_disagreeing_with_trades(data):
    def corrupt(d):
        d["positions"][0]["quantity"] += Decimal("1")
    assert any("net trades" in f for f in _mutated(data, corrupt))


def test_detects_contact_by_another_clients_rm(data):
    def corrupt(d):
        x = d["CLIENT_CONTACTS"][0]
        x["RM_ID"] = next(r["RM_ID"] for r in d["RELATIONSHIP_MANAGERS"] if r["RM_ID"] != x["RM_ID"])
    assert any("another client's RM" in f for f in _mutated(data, corrupt))


def test_detects_unfunded_goal(data):
    def corrupt(d):
        gid = d["portfolio_goals"][0]["goal_id"]
        for a in d["investment_accounts"]:
            if a["goal_id"] == gid:
                a["goal_id"] = None
    assert any("no funding account" in f for f in _mutated(data, corrupt))


def test_every_private_banking_client_has_wealth_accounts(data):
    """The coverage gap that left 7 of 10 demo answers with a blank balance."""
    pb = {c["CLIENT_ID"] for c in data["CLIENTS"] if c["SEGMENT"] == "PRIVATE_BANKING"}
    covered = {a["client_id"] for a in data["investment_accounts"]}
    assert pb <= covered


def test_retirement_question_has_answers_on_both_sides(data):
    s = g.story(data)
    assert 0 < s["retirement_goals_on_target"] < s["retirement_goals"]
    assert s["private_banking_clients_behind_on_retirement"] > 0
    assert s["term_deposits_maturing_next_month"] > 0


def test_detects_planned_retirement_disagreeing_with_the_goal(data):
    def corrupt(d):
        e = next(e for e in d["CLIENT_LIFE_EVENTS"] if e["EVENT_TYPE"] == "PLANNED_RETIREMENT")
        e["EVENT_DATE"] = e["EVENT_DATE"].replace(year=e["EVENT_DATE"].year + 1)
    assert any("retirement goal date" in f for f in _mutated(data, corrupt))


def test_detects_college_start_with_no_child(data):
    def corrupt(d):
        e = next(e for e in d["CLIENT_LIFE_EVENTS"] if e["EVENT_TYPE"] == "CHILD_TO_COLLEGE")
        d["CLIENT_LIFE_EVENTS"] = [x for x in d["CLIENT_LIFE_EVENTS"]
                                   if not (x["CLIENT_ID"] == e["CLIENT_ID"] and x["EVENT_TYPE"] == "NEW_CHILD")]
    assert any("no matching child" in f for f in _mutated(data, corrupt))


def test_making_life_events_changes_no_existing_row(data):
    """They are derived from the other tables and must not edit them."""
    import copy

    from demo_dataset import generate as gen
    out = copy.deepcopy({k: v for k, v in data.items() if k != "CLIENT_LIFE_EVENTS"})
    before = copy.deepcopy(out)
    assert gen._life_events(out)
    assert out == before
