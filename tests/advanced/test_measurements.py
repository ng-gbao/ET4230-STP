import csv
import json
from pathlib import Path

import pytest

from stp.experiments import connectivity_restoration, export, run
from stp.simulator import BridgeID, Simulation, Timers


CONFIG = json.loads((Path(__file__).parents[2] / "experiments/triangle.json").read_text())


@pytest.mark.parametrize(("case", "control", "overall", "restoration"), [
    ("normal", 20, 30000, 30000),
    ("priority_change", 20, 30000, 30000),
    ("link_failure", 0, 30000, 30000),
    ("blocked_link_failure", 0, 0, 0),
    ("higher_path_cost", 20, 30000, 30000),
    ("short_forward_delay", 20, 24000, 24000),
])
def test_metrics_against_trace_and_actual_forwarding_graph(case, control, overall, restoration):
    result = next(row for row in run(CONFIG) if row["experiment"] == case)
    trigger = result["trigger_ms"]
    trace = result["timeline"]
    latest_control = max((event["time_ms"] for event in trace
                          if event["kind"] in {"bridge_id", "root_selection", "port_role"}),
                         default=trigger)
    latest_port = max((event["time_ms"] for event in trace if event["kind"] == "port_state"),
                      default=trigger)
    assert result["control_convergence_ms"] == latest_control - trigger == control
    assert result["port_state_convergence_ms"] == latest_port - trigger
    assert result["convergence_ms"] == max(latest_control, latest_port) - trigger == overall
    assert result["observed_until_ms"] - max(event["time_ms"] for event in trace) >= 20000
    assert result["stability_window_ms"] == 20000

    # Re-run a fresh simulation and inspect the live graph after each reported timestamp.
    links = [(*link[:-1], result["path_costs"][link[0]]) for link in CONFIG["links"]]
    sim = Simulation(CONFIG["bridges"], links, result["propagation_ms"],
                     timers=Timers(**result["timers_ms"]))
    if trigger:
        sim.run(trigger)
        config = next(row for row in CONFIG["experiments"] if row["name"] == case)
        if "priority" in config:
            sim.set_priority(*config["priority"])
        if "fail_link" in config:
            sim.set_link(config["fail_link"], False)

    connected_since = None
    for when in sorted({trigger, *(event["time_ms"] for event in trace)}):
        sim.run(when)
        if not sim.snapshot()["connected"]:
            connected_since = None
        elif connected_since is None:
            connected_since = when
    assert result["connectivity_restoration_ms"] == connected_since - trigger == restoration


def test_restoration_uses_final_connected_interval_and_completes_same_time_events():
    bridges = ("a", "b")
    links = [("first", "a", 1, "b", 1, 19), ("second", "a", 2, "b", 2, 19)]
    before = {"connected": True, "ports": [
        {"bridge": bridge, "port": 1, "state": "forwarding"} for bridge in bridges
    ]}
    changes = [
        {"time_ms": 10, "kind": "port_state", "bridge": "a", "port": 1, "state": "blocking"},
        {"time_ms": 10, "kind": "port_state", "bridge": "a", "port": 2, "state": "forwarding"},
        {"time_ms": 10, "kind": "port_state", "bridge": "b", "port": 2, "state": "forwarding"},
        {"time_ms": 20, "kind": "port_state", "bridge": "b", "port": 2, "state": "blocking"},
        {"time_ms": 30, "kind": "port_state", "bridge": "b", "port": 2, "state": "forwarding"},
        {"time_ms": 40, "kind": "port_state", "bridge": "b", "port": 1, "state": "disabled"},
    ]
    assert connectivity_restoration(bridges, links, changes[:3], before, 10) == 0
    assert connectivity_restoration(bridges, links, changes[:4], before, 10) is None
    assert connectivity_restoration(bridges, links, changes, before, 10) == 20


def test_artifact_schema_agrees_with_configuration_and_other_exports(tmp_path):
    export(run(CONFIG), tmp_path)
    data = json.loads((tmp_path / "summary.json").read_text())
    with (tmp_path / "summary.csv").open(newline="") as file:
        summary = list(csv.DictReader(file))
    with (tmp_path / "ports.csv").open(newline="") as file:
        ports = list(csv.DictReader(file))

    cases = [case["name"] for case in CONFIG["experiments"]]
    assert cases == [item["experiment"] for item in data] == [row["experiment"] for row in summary]
    for result, row in zip(data, summary, strict=True):
        assert row["root_id"] == result["root_id"]
        assert row["time_basis"] == result["time_basis"] == "simulated"
        for field in ("convergence_ms", "connectivity_restoration_ms", "stability_window_ms"):
            assert int(row[field]) == result[field]
        for state in ("forwarding", "blocking", "disabled"):
            assert int(row[state]) == result["state_counts"][state]
        assert [{key: val for key, val in port.items() if key != "experiment"}
                for port in ports if port["experiment"] == row["experiment"]] == [
            {key: str(val) for key, val in port.items()} for port in result["ports"]
        ]


def test_tied_events_follow_insertion_order_even_with_new_same_time_event():
    sim = Simulation({"s1": CONFIG["bridges"]["s1"]}, [])
    order = []

    def first():
        order.append("first")
        sim.schedule(sim.now, order.append, "third")

    sim.schedule(10, first)
    sim.schedule(10, order.append, "second")
    sim.run(9)
    assert order == []
    sim.run(10)
    assert order == ["first", "second", "third"]


@pytest.mark.parametrize("priority", [0.0, 4096.0, False])
def test_bridge_priority_must_be_an_integer(priority):
    with pytest.raises(ValueError, match="priority"):
        BridgeID(priority, "02:00:00:00:00:01")


@pytest.mark.parametrize("link", [
    ("bad-cost", "s1", 1, "s2", 1, True),
    ("bad-port", "s1", True, "s2", 1, 19),
    ("unknown-peer", "s1", 1, "missing", 1, 19),
])
def test_topology_rejects_invalid_link_fields(link):
    with pytest.raises(ValueError, match=link[0]):
        Simulation(CONFIG["bridges"], [link])
