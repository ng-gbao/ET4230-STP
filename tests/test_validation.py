import json
from pathlib import Path

from stp.experiments import export, run
from stp.plot import comparison
from stp.simulator import Simulation, Timers


CONFIG = json.loads((Path(__file__).parents[1] / "experiments/triangle.json").read_text())


def new_simulation():
    return Simulation(CONFIG["bridges"], CONFIG["links"], CONFIG["propagation_ms"])


def port_at(snapshot, bridge, number):
    return next(port for port in snapshot["ports"]
                if (port["bridge"], port["port"]) == (bridge, number))


def test_isolated_bridge_is_own_root_without_sending_bpdus():
    sim = Simulation({"s1": CONFIG["bridges"]["s1"]}, [])
    sim.run(90000)
    result = sim.snapshot()
    assert result["bridges"]["s1"]["root_id"] == result["bridges"]["s1"]["bridge_id"]
    assert result["bridges"]["s1"]["root_port"] is None
    assert result["connected"] and result["loop_free"]
    assert sim.sent == 0


def test_line_propagates_root_one_link_delay_at_a_time():
    links = [link for link in CONFIG["links"] if link[0] != "s1-s3"]
    sim = Simulation(CONFIG["bridges"], links)
    sim.run(19)
    assert sim.bridges["s3"].root == sim.bridges["s2"].identifier
    sim.run(20)
    assert sim.bridges["s3"].root == sim.bridges["s1"].identifier
    assert sim.bridges["s3"].cost == 38
    sim.run(30000)
    result = sim.snapshot()
    assert result["connected"] and result["loop_free"]
    assert result["forwarding_links"] == ["s1-s2", "s2-s3"]
    assert result["state_counts"]["forwarding"] == 4


def test_triangle_designated_port_tie_uses_sender_bridge_id():
    sim = new_simulation()
    sim.run(90000)
    result = sim.snapshot()
    assert result["bridges"]["s2"]["root_cost"] == result["bridges"]["s3"]["root_cost"]
    assert port_at(result, "s2", 2)["role"] == "designated"
    loser = port_at(result, "s3", 2)
    assert (loser["role"], loser["state"]) == ("alternate", "blocking")


def test_equal_path_cost_prefers_sender_bridge_before_port_number():
    bridges = {
        **CONFIG["bridges"],
        "s4": {"priority": 32768, "mac": "02:00:00:00:00:04"},
    }
    links = [
        ("s1-s2", "s1", 1, "s2", 1, 19),
        ("s1-s3", "s1", 2, "s3", 2, 19),
        ("s2-s4", "s2", 3, "s4", 2, 19),
        ("s3-s4", "s3", 1, "s4", 1, 19),
    ]
    sim = Simulation(bridges, links)
    sim.run(90000)
    result = sim.snapshot()
    assert result["bridges"]["s4"]["root_cost"] == 38
    assert result["bridges"]["s4"]["root_port"] == 2
    assert port_at(result, "s4", 1)["state"] == "blocking"
    assert result["connected"] and result["loop_free"]
    assert len(result["forwarding_links"]) == 3


def test_loop_checker_counts_parallel_forwarding_edges():
    bridges = {name: CONFIG["bridges"][name] for name in ("s1", "s2")}
    links = [(f"link{n}", "s1", n, "s2", n, 19) for n in range(1, 4)]
    sim = Simulation(bridges, links)
    sim.run(90000)
    result = sim.snapshot()
    assert result["forwarding_links"] == ["link1"]
    assert result["state_counts"]["blocking"] == 2
    assert result["loop_free"]

    # Inject a second forwarding edge to exercise the checker on a multigraph.
    sim.bridges["s2"].ports[2].state = "forwarding"
    result = sim.snapshot()
    assert result["connected"]
    assert result["forwarding_links"] == ["link1", "link2"]
    assert not result["loop_free"]


def test_periodic_bpdus_refresh_the_original_expiration(monkeypatch):
    # Send at t=0 and t=hello, then cut off periodic transmission while the link stays up.
    original_hello = Simulation._hello

    def send_two_hellos(self):
        if self.now <= self.timers.hello:
            original_hello(self)

    monkeypatch.setattr(Simulation, "_hello", send_two_hellos)
    bridges = {name: CONFIG["bridges"][name] for name in ("s1", "s2")}
    sim = Simulation(bridges, [CONFIG["links"][0]])
    sim.run(sim.timers.max_age)
    assert sim.bridges["s2"].root == sim.bridges["s1"].identifier
    expiry = sim.timers.hello + sim.timers.max_age
    sim.run(expiry - 1)
    assert sim.bridges["s2"].root_port == 1
    sim.run(expiry)
    assert sim.bridges["s2"].root == sim.bridges["s2"].identifier
    assert sim.bridges["s2"].root_port is None
    assert sim.last_change == expiry


def test_link_failure_and_recovery_change_connectivity_and_restore_tree():
    sim = new_simulation()
    sim.run(90000)
    before = sim.snapshot()
    sim.schedule(90001, sim.set_link, "s1-s3", False)
    sim.run(90001)
    assert not sim.snapshot()["connected"]
    assert port_at(sim.snapshot(), "s3", 1)["state"] == "disabled"
    assert port_at(sim.snapshot(), "s3", 2)["state"] == "listening"
    sim.run(150000)
    failed = sim.snapshot()
    assert failed["connected"] and failed["loop_free"]
    assert failed["forwarding_links"] == ["s1-s2", "s2-s3"]

    sim.schedule(150010, sim.set_link, "s1-s3", True)
    sim.run(150010)
    assert port_at(sim.snapshot(), "s3", 1)["state"] == "listening"
    sim.run(210000)
    assert sim.snapshot() == before


def test_raising_priority_again_restores_original_root():
    sim = new_simulation()
    sim.run(90000)
    before = sim.snapshot()
    sim.schedule(90000, sim.set_priority, "s3", 4096)
    sim.run(90009)
    assert sim.bridges["s1"].root == sim.bridges["s1"].identifier
    sim.run(90010)
    assert sim.bridges["s1"].root == sim.bridges["s3"].identifier
    sim.run(150000)
    assert sim.snapshot()["forwarding_links"] == ["s1-s3", "s2-s3"]
    sim.set_priority("s3", 32768)
    sim.run(210000)
    assert sim.snapshot() == before


def test_short_forward_delay_times_both_state_transitions():
    timers = Timers(forward_delay=12000)
    sim = Simulation(CONFIG["bridges"], CONFIG["links"], timers=timers)
    for timestamp, state in ((0, "listening"), (11999, "listening"),
                             (12000, "learning"), (23999, "learning"),
                             (24000, "forwarding")):
        sim.run(timestamp)
        assert port_at(sim.snapshot(), "s1", 1)["state"] == state


def test_experiment_timing_matches_observed_changes():
    for result in run(CONFIG):
        timeline = result["timeline"]
        assert timeline == sorted(timeline, key=lambda change: change["time_ms"])
        assert result["convergence_ms"] == timeline[-1]["time_ms"] - result["trigger_ms"]
        assert result["connected"] and result["loop_free"]
        assert result["convergence_ms"] == max(result["control_convergence_ms"],
                                               result["port_state_convergence_ms"])
        if result["phase"] == "reconvergence":
            assert result["baseline_convergence_ms"] < result["trigger_ms"]
        else:
            assert result["baseline_convergence_ms"] is None


def test_variations_affect_timers_or_forwarding_paths():
    results = {row["experiment"]: row for row in run(CONFIG)}
    normal = results["normal"]
    assert results["higher_path_cost"]["bridges"]["s3"]["root_cost"] == 38
    assert results["higher_path_cost"]["forwarding_links"] != normal["forwarding_links"]
    assert results["blocked_link_failure"]["forwarding_links"] == normal["forwarding_links"]
    assert results["blocked_link_failure"]["convergence_ms"] == 0
    short = results["short_forward_delay"]
    assert short["convergence_ms"] == 2 * short["timers_ms"]["forward_delay"]
    assert short["convergence_ms"] < normal["convergence_ms"]


def test_repeated_experiments_export_identical_artifacts(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    results_a = run(CONFIG)
    results_b = run(CONFIG)
    assert results_a == results_b
    export(results_a, first)
    export(results_b, second)
    assert comparison(results_a) == comparison(results_b)
    assert {path.name for path in first.iterdir()} == {"summary.csv", "summary.json", "ports.csv"}
    for path in first.iterdir():
        assert path.read_bytes() == (second / path.name).read_bytes()
