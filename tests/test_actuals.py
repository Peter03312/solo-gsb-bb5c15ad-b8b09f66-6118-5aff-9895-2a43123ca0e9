"""End-to-end tests for the departure-actual audit module.

POST /api/v1/batches/{batch_id}/actuals
GET  /api/v1/batches/{batch_id}/actual-audit
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _flight(no: str, off: str, wake: str) -> dict:
    return {"flight_no": no, "scheduled_off": off, "wake": wake}


def _actual(no: str, off: str, revision: int) -> dict:
    return {"flight_no": no, "actual_off": off, "revision": revision}


def _validate(client, body: dict) -> str:
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 200, r.text
    return r.json()["batch_id"]


def _post(client, batch_id: str, items: list[dict]):
    return client.post(
        f"/api/v1/batches/{batch_id}/actuals", json={"actuals": items}
    )


def _audit(client, batch_id: str):
    r = client.get(f"/api/v1/batches/{batch_id}/actual-audit")
    assert r.status_code == 200, r.text
    return r.json()


# ---------- batch-level errors ----------


def test_unknown_batch_returns_404(client):
    r = client.get("/api/v1/batches/unknown/actual-audit")
    assert r.status_code == 404
    assert "批次不存在" in r.text
    r = _post(
        client,
        "unknown",
        [_actual("CA1", "2026-09-18T00:00:00Z", 1)],
    )
    assert r.status_code == 404
    assert "批次不存在" in r.text


def test_non_validate_batch_get_returns_422(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:00:50Z", "M"),
                ],
            }
        ]
    }
    trial_id = client.post("/api/v1/trial", json=body).json()["batch_id"]
    swap_id = client.post(
        "/api/v1/trial/best-adjacent-swap", json=body
    ).json()["batch_id"]
    joint_id = client.post("/api/v1/trial/joint", json=body).json()["batch_id"]
    for batch_id in (trial_id, swap_id, joint_id):
        r = client.get(f"/api/v1/batches/{batch_id}/actual-audit")
        assert r.status_code == 422, batch_id
        assert "validate" in r.text


def test_non_validate_batch_post_returns_422(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:00:50Z", "M"),
                ],
            }
        ]
    }
    trial_id = client.post("/api/v1/trial", json=body).json()["batch_id"]
    r = _post(client, trial_id, [_actual("CA1", "2026-09-18T00:00:00Z", 1)])
    assert r.status_code == 422
    assert "validate" in r.text


# ---------- request validation ----------


def test_actual_off_requires_timezone(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    ],
                }
            ]
        },
    )
    r = _post(
        client,
        batch_id,
        [{"flight_no": "CA1", "actual_off": "2026-09-18T00:00:00", "revision": 1}],
    )
    assert r.status_code == 422


@pytest.mark.parametrize("revision", [0, -1, 1.5, "1", True])
def test_revision_must_be_positive_integer(client, revision):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    ],
                }
            ]
        },
    )
    r = _post(
        client,
        batch_id,
        [
            {
                "flight_no": "CA1",
                "actual_off": "2026-09-18T00:00:00Z",
                "revision": revision,
            }
        ],
    )
    assert r.status_code == 422


def test_empty_actuals_batch_rejected(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    ],
                }
            ]
        },
    )
    r = client.post(f"/api/v1/batches/{batch_id}/actuals", json={"actuals": []})
    assert r.status_code == 422


def test_unknown_flight_returns_422(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    ],
                }
            ]
        },
    )
    r = _post(client, batch_id, [_actual("ZZ9", "2026-09-18T00:00:00Z", 1)])
    assert r.status_code == 422
    assert "ZZ9" in r.text
    # nothing landed
    audit = _audit(client, batch_id)
    assert audit["total_reported"] == 0


def test_duplicate_flight_in_same_request_returns_422(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    ],
                }
            ]
        },
    )
    r = _post(
        client,
        batch_id,
        [
            _actual("CA1", "2026-09-18T00:00:00Z", 1),
            _actual("CA1", "2026-09-18T00:00:10Z", 1),
        ],
    )
    assert r.status_code == 422
    assert "重复" in r.text
    assert _audit(client, batch_id)["total_reported"] == 0


def test_unknown_flight_on_other_runway_rolls_back_valid_row(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    ],
                },
                {
                    "runway": "18R",
                    "flights": [
                        _flight("MU1", "2026-09-18T00:00:00Z", "M"),
                    ],
                },
            ]
        },
    )
    # CA1 is valid but NOPE is unknown: the whole batch is rejected and the
    # otherwise-valid CA1 row is not stored.
    r = _post(
        client,
        batch_id,
        [
            _actual("CA1", "2026-09-18T00:00:00Z", 1),
            _actual("NOPE", "2026-09-18T00:00:00Z", 1),
        ],
    )
    assert r.status_code == 422
    assert "NOPE" in r.text
    assert _audit(client, batch_id)["total_reported"] == 0


# ---------- revision semantics ----------


def _single_runway_batch(client, **runway_kw):
    flights = runway_kw.pop("flights")
    return _validate(
        client, {"runways": [{"runway": "36L", "flights": flights, **runway_kw}]}
    )


def test_first_report_created_and_listed_in_revision_order(client):
    batch_id = _single_runway_batch(
        client,
        flights=[
            _flight("CA1", "2026-09-18T00:00:00Z", "J"),
            _flight("CA2", "2026-09-18T00:01:00Z", "M"),
            _flight("CA3", "2026-09-18T00:02:00Z", "M"),
        ],
    )
    r = _post(
        client,
        batch_id,
        [
            _actual("CA3", "2026-09-18T00:02:05Z", 3),
            _actual("CA1", "2026-09-18T00:00:09Z", 1),
        ],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # accepted echo follows the request order
    assert [a["flight_no"] for a in data["accepted"]] == ["CA3", "CA1"]
    assert {a["status"] for a in data["accepted"]} == {"created"}

    rw = data["runways"][0]
    # reported order is (actual_off, flight_no): CA1@00:00:09 then CA3@00:02:05
    assert rw["order"] == ["CA1", "CA3"]
    assert rw["reported_count"] == 2
    assert [(c["flight_no"], c["revision"]) for c in rw["current_revisions"]] == [
        ("CA1", 1),
        ("CA3", 3),
    ]
    assert [u["flight_no"] for u in rw["unreported"]] == ["CA2"]
    assert rw["unreported"][0]["planned_position"] == 2
    assert data["total_reported"] == 2
    assert data["total_unreported"] == 1


def test_equal_revision_equal_value_is_idempotent(client):
    batch_id = _single_runway_batch(
        client, flights=[_flight("CA1", "2026-09-18T00:00:00Z", "M")]
    )
    payload = [_actual("CA1", "2026-09-18T08:00:00+08:00", 1)]
    assert _post(client, batch_id, payload).json()["accepted"][0]["status"] == "created"
    # same revision, same instant expressed in a different timezone
    second = _post(
        client, batch_id, [_actual("CA1", "2026-09-18T00:00:00Z", 1)]
    )
    assert second.status_code == 200
    assert second.json()["accepted"][0]["status"] == "unchanged"


def test_equal_revision_different_value_returns_409(client):
    batch_id = _single_runway_batch(
        client, flights=[_flight("CA1", "2026-09-18T00:00:00Z", "M")]
    )
    assert (
        _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:00Z", 1)]).status_code
        == 200
    )
    r = _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:01Z", 1)])
    assert r.status_code == 409
    assert "修订" in r.text
    cur = _audit(client, batch_id)["runways"][0]["current_revisions"][0]
    assert cur["actual_off"] == "2026-09-18T00:00:00Z"  # stored value unchanged


def test_lower_revision_returns_409(client):
    batch_id = _single_runway_batch(
        client, flights=[_flight("CA1", "2026-09-18T00:00:00Z", "M")]
    )
    assert (
        _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:10Z", 5)]).status_code
        == 200
    )
    r = _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:00Z", 4)])
    assert r.status_code == 409
    cur = _audit(client, batch_id)["runways"][0]["current_revisions"][0]
    assert cur["revision"] == 5
    assert cur["actual_off"] == "2026-09-18T00:00:10Z"


def test_higher_revision_replaces(client):
    batch_id = _single_runway_batch(
        client, flights=[_flight("CA1", "2026-09-18T00:00:00Z", "M")]
    )
    _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:10Z", 1)])
    r = _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:40Z", 2)])
    assert r.status_code == 200
    assert r.json()["accepted"][0]["status"] == "replaced"
    cur = _audit(client, batch_id)["runways"][0]["current_revisions"][0]
    assert cur["revision"] == 2
    assert cur["actual_off"] == "2026-09-18T00:00:40Z"


def test_conflict_rolls_back_otherwise_valid_rows_in_same_batch(client):
    batch_id = _single_runway_batch(
        client,
        flights=[
            _flight("CA1", "2026-09-18T00:00:00Z", "M"),
            _flight("CA2", "2026-09-18T00:01:00Z", "M"),
        ],
    )
    _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:00Z", 2)])
    # CA2 would be new, but CA1's lower revision rejects the whole request
    r = _post(
        client,
        batch_id,
        [
            _actual("CA2", "2026-09-18T00:01:00Z", 1),
            _actual("CA1", "2026-09-18T00:00:05Z", 1),
        ],
    )
    assert r.status_code == 409
    audit = _audit(client, batch_id)
    assert audit["total_reported"] == 1
    assert audit["runways"][0]["order"] == ["CA1"]
    assert [u["flight_no"] for u in audit["runways"][0]["unreported"]] == ["CA2"]


# ---------- recomputation: wake / closure / capacity ----------


def test_wake_violation_recomputed_on_actual_times_only(client):
    batch_id = _single_runway_batch(
        client,
        flights=[
            _flight("CA1", "2026-09-18T00:00:00Z", "J"),
            _flight("CA2", "2026-09-18T00:10:00Z", "M"),  # planned clean
            _flight("CA3", "2026-09-18T00:20:00Z", "M"),  # unreported
        ],
    )
    # actual CA2 only 30s behind J -> J->M 180 violation; CA3 not considered
    r = _post(
        client,
        batch_id,
        [
            _actual("CA1", "2026-09-18T00:00:00Z", 1),
            _actual("CA2", "2026-09-18T00:00:30Z", 1),
        ],
    )
    rw = r.json()["runways"][0]
    assert len(rw["violations"]) == 1
    v = rw["violations"][0]
    assert (v["leader"], v["follower"]) == ("CA1", "CA2")
    assert v["required_seconds"] == 180
    assert v["actual_seconds"] == 30
    assert v["shortfall_seconds"] == 150
    assert v["leader_actual_off"] == "2026-09-18T00:00:00Z"
    assert v["follower_actual_off"] == "2026-09-18T00:00:30Z"
    assert r.json()["total_violations"] == 1


def test_late_report_between_two_flights_changes_adjacency(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                        _flight("CA2", "2026-09-18T00:00:20Z", "M"),
                        _flight("CA3", "2026-09-18T00:00:40Z", "L"),
                    ],
                }
            ],
            # only J->L is constrained (300s); CA1->CA3 would violate if adjacent
            "matrix": [{"leader": "J", "follower": "L", "seconds": 300}],
        },
    )
    # report CA1 and CA3: without CA2 they are adjacent -> 40s < 300 violation
    r = _post(
        client,
        batch_id,
        [
            _actual("CA1", "2026-09-18T00:00:00Z", 1),
            _actual("CA3", "2026-09-18T00:00:40Z", 1),
        ],
    )
    rw = r.json()["runways"][0]
    assert [(v["leader"], v["follower"]) for v in rw["violations"]] == [
        ("CA1", "CA3")
    ]
    assert [u["flight_no"] for u in rw["unreported"]] == ["CA2"]

    # late report CA2 landing between them -> adjacency CA1-CA2-CA3; J->L gap
    # is no longer between adjacent flights, the old violation disappears.
    r = _post(
        client,
        batch_id,
        [_actual("CA2", "2026-09-18T00:00:20Z", 1)],
    )
    rw = r.json()["runways"][0]
    assert rw["order"] == ["CA1", "CA2", "CA3"]
    assert rw["violations"] == []
    assert r.json()["total_violations"] == 0
    assert rw["unreported"] == []


def test_correction_removes_violation_and_closure_conflict(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                        _flight("CA2", "2026-09-18T00:10:00Z", "M"),
                    ],
                    "closures": [
                        {
                            "start": "2026-09-18T00:05:00Z",
                            "end": "2026-09-18T00:06:00Z",
                        }
                    ],
                }
            ],
            # require 400s behind J: actual 330s is both a wake violation and
            # inside the frozen closure window.
            "matrix": [{"leader": "J", "follower": "M", "seconds": 400}],
        },
    )
    _post(
        client,
        batch_id,
        [
            _actual("CA1", "2026-09-18T00:00:00Z", 1),
            _actual("CA2", "2026-09-18T00:05:30Z", 1),  # in closure, 330s behind J
        ],
    )
    audit = _audit(client, batch_id)
    rw = audit["runways"][0]
    assert len(rw["violations"]) == 1
    assert rw["violations"][0]["required_seconds"] == 400
    assert [c["flight_no"] for c in rw["closure_conflicts"]] == ["CA2"]

    # correction pushes CA2 out of the closure and beyond the 400s gap
    r = _post(
        client,
        batch_id,
        [_actual("CA2", "2026-09-18T00:07:00Z", 2)],
    )
    rw = r.json()["runways"][0]
    assert rw["violations"] == []
    assert rw["closure_conflicts"] == []
    assert r.json()["total_violations"] == 0
    assert r.json()["total_closure_conflicts"] == 0
    # GET recomputes to the same audit payload (POST adds the accepted echo)
    post_audit = {k: v for k, v in r.json().items() if not k.startswith("accepted")}
    assert _audit(client, batch_id) == post_audit


def test_capacity_window_recomputed_on_reported_subset(client):
    batch_id = _single_runway_batch(
        client,
        flights=[
            _flight("CA1", "2026-09-18T00:00:00Z", "M"),
            _flight("CA2", "2026-09-18T00:01:00Z", "M"),
            _flight("CA3", "2026-09-18T00:02:00Z", "M"),
        ],
        capacity={"window_seconds": 600, "max_departures": 2},
    )
    # only CA1 and CA3 reported, 120s apart: window 600s / 2 flights is fine
    r = _post(
        client,
        batch_id,
        [
            _actual("CA1", "2026-09-18T00:00:00Z", 1),
            _actual("CA3", "2026-09-18T00:02:00Z", 1),
        ],
    )
    rw = r.json()["runways"][0]
    assert rw["capacity_violations"] == []
    assert "capacity" in rw

    # now CA2 reports between them at 60s -> CA3 overflows the window
    # anchored at CA1 [00:00, 00:10)
    r = _post(
        client,
        batch_id,
        [_actual("CA2", "2026-09-18T00:01:00Z", 1)],
    )
    rw = r.json()["runways"][0]
    assert len(rw["capacity_violations"]) == 1
    cv = rw["capacity_violations"][0]
    assert cv["flight_no"] == "CA3"
    assert cv["anchor_flight_no"] == "CA1"
    assert cv["window_seconds"] == 600
    assert cv["max_departures"] == 2
    assert cv["departures_in_window"] == 3
    assert r.json()["total_capacity_violations"] == 1

    # correcting CA2 later than CA3 removes the overflow (order CA1,CA3,CA2
    # with CA2 at +15min: no 600s window holds 3 flights)
    r = _post(
        client,
        batch_id,
        [_actual("CA2", "2026-09-18T00:20:00Z", 2)],
    )
    rw = r.json()["runways"][0]
    assert rw["order"] == ["CA1", "CA3", "CA2"]
    assert rw["capacity_violations"] == []
    assert r.json()["total_capacity_violations"] == 0


def test_capacity_fields_absent_without_frozen_capacity(client):
    batch_id = _single_runway_batch(
        client, flights=[_flight("CA1", "2026-09-18T00:00:00Z", "M")]
    )
    r = _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:00Z", 1)])
    rw = r.json()["runways"][0]
    assert "capacity" not in rw
    assert "capacity_violations" not in rw
    assert "total_capacity_violations" not in r.json()
    assert "capacity" not in _audit(client, batch_id)["runways"][0]


def test_runways_are_independent_and_sorted(client):
    batch_id = _validate(
        client,
        {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                        _flight("CA2", "2026-09-18T00:00:30Z", "M"),  # J->M 180
                    ],
                },
                {
                    "runway": "18R",
                    "flights": [
                        _flight("MU1", "2026-09-18T00:00:00Z", "H"),
                        _flight("MU2", "2026-09-18T00:00:30Z", "M"),  # H->M 120
                    ],
                },
            ]
        },
    )
    # report one flight per runway: no adjacent pairs -> no violations
    r = _post(
        client,
        batch_id,
        [
            _actual("CA1", "2026-09-18T00:00:00Z", 1),
            _actual("MU1", "2026-09-18T00:00:00Z", 1),
        ],
    )
    data = r.json()
    assert [rw["runway"] for rw in data["runways"]] == ["18R", "36L"]
    for rw in data["runways"]:
        assert rw["violations"] == []
        assert rw["reported_count"] == 1

    # second report on each runway: per-runway violations, not cross-runway
    r = _post(
        client,
        batch_id,
        [
            _actual("CA2", "2026-09-18T00:00:30Z", 1),
            _actual("MU2", "2026-09-18T00:00:30Z", 1),
        ],
    )
    counts = {rw["runway"]: len(rw["violations"]) for rw in r.json()["runways"]}
    assert counts == {"18R": 1, "36L": 1}


def test_timezone_offsets_normalised_to_utc(client):
    batch_id = _single_runway_batch(
        client, flights=[_flight("CA1", "2026-09-18T00:00:00Z", "M")]
    )
    r = _post(
        client,
        batch_id,
        [_actual("CA1", "2026-09-18T08:00:00+08:00", 1)],
    )
    assert r.status_code == 200
    cur = r.json()["runways"][0]["current_revisions"][0]
    assert cur["actual_off"] == "2026-09-18T00:00:00Z"


def test_same_second_actuals_tie_on_flight_number(client):
    batch_id = _single_runway_batch(
        client,
        flights=[
            _flight("AAA", "2026-09-18T00:01:00Z", "M"),
            _flight("ZZZ", "2026-09-18T00:02:00Z", "M"),
        ],
    )
    # both actuals land in the same whole second, one with a sub-second part;
    # the stable order ties on flight number, not on the microsecond remainder.
    r = _post(
        client,
        batch_id,
        [
            _actual("ZZZ", "2026-09-18T00:00:00.900Z", 1),
            _actual("AAA", "2026-09-18T00:00:00.100Z", 1),
        ],
    )
    rw = r.json()["runways"][0]
    assert rw["order"] == ["AAA", "ZZZ"]
    assert [c["flight_no"] for c in rw["current_revisions"]] == ["AAA", "ZZZ"]
    assert [c["position"] for c in rw["current_revisions"]] == [1, 2]


# ---------- determinism, restart, frozen plan ----------


def test_get_is_read_only_and_replayable(client):
    batch_id = _single_runway_batch(
        client,
        flights=[
            _flight("CA1", "2026-09-18T00:00:00Z", "J"),
            _flight("CA2", "2026-09-18T00:00:30Z", "M"),
        ],
    )
    _post(client, batch_id, [_actual("CA1", "2026-09-18T00:00:00Z", 1)])
    first = _audit(client, batch_id)
    assert _audit(client, batch_id) == first
    # the original validate batch is untouched
    batch = client.get(f"/api/v1/batches/{batch_id}").json()
    assert batch["mode"] == "validate"
    assert batch["total_violations"] == 1
    assert [f["flight_no"] for f in batch["runways"][0].get("locked_flights", [])] == []


def test_audit_consistent_after_restart(tmp_path, monkeypatch):
    db_path = str(tmp_path / "restart.db")
    monkeypatch.setenv("WAKE_DB_PATH", db_path)
    from app import database
    from app.main import app as fastapi_app

    database.init_db(db_path)
    with TestClient(fastapi_app) as first_client:
        body = {
            "runways": [
                {
                    "runway": "36L",
                    "flights": [
                        _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                        _flight("CA2", "2026-09-18T00:10:00Z", "M"),
                    ],
                    "closures": [
                        {
                            "start": "2026-09-18T00:05:00Z",
                            "end": "2026-09-18T00:06:00Z",
                        }
                    ],
                    "capacity": {"window_seconds": 600, "max_departures": 2},
                }
            ]
        }
        batch_id = first_client.post("/api/v1/validate", json=body).json()[
            "batch_id"
        ]
        _post(
            first_client,
            batch_id,
            [
                _actual("CA1", "2026-09-18T00:00:00Z", 1),
                _actual("CA2", "2026-09-18T00:05:30Z", 1),
            ],
        )
        before = _audit(first_client, batch_id)

    # simulate a process restart: new connection pool, schema re-applied
    database.init_db(db_path)
    with TestClient(fastapi_app) as second_client:
        after = _audit(second_client, batch_id)
    assert after == before
    assert after["runways"][0]["closure_conflicts"][0]["flight_no"] == "CA2"
    assert [
        c["flight_no"] for c in after["runways"][0]["current_revisions"]
    ] == ["CA1", "CA2"]
