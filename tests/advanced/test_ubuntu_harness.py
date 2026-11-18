"""Static harness tests using fake OVSDB responses and Mininet nodes."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ubuntu import validate_stp as harness


CONFIG = json.loads((Path(__file__).parents[2] / "experiments/triangle.json").read_text())


def test_ovs_commands_have_both_timeouts_and_capture_output(monkeypatch):
    def call(args, **options):
        assert args == ["ovs-vsctl", "--timeout=5", "list-br"]
        assert options == {"text": True, "check": True, "capture_output": True, "timeout": 7}
        return subprocess.CompletedProcess(args, 0, "s1\n", "")

    monkeypatch.setattr(harness.subprocess, "run", call)
    assert harness.ovs("list-br") == "s1\n"


def test_ovs_errors_include_stderr_and_timeout(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="database unavailable\n")

    monkeypatch.setattr(harness.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        harness.ovs("show")

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 7)

    monkeypatch.setattr(harness.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="process timeout"):
        harness.ovs("show")


def test_ovs_status_uses_ovsdb_headings_not_column_positions(monkeypatch):
    def response(*args):
        assert args == ("--format=json", "--columns=name,status", "list", "Bridge")
        return json.dumps({
            "headings": ["status", "name"],
            "data": [[["map", [["stp_root_path_cost", "19"]]], "s2"], [["map", []], "s3"]],
        })

    monkeypatch.setattr(harness, "ovs", response)
    assert harness.ovs_status("Bridge") == {"s2": {"stp_root_path_cost": "19"}, "s3": {}}


@pytest.mark.parametrize("status", [None, ["set", []], ["map", ["invalid"]]])
def test_ovs_status_errors_identify_the_table_and_row(monkeypatch, status):
    monkeypatch.setattr(harness, "ovs", lambda *_: json.dumps({
        "headings": ["name", "status"], "data": [["s1", status]],
    }))
    with pytest.raises(RuntimeError, match="OVSDB status for Bridge s1"):
        harness.ovs_status("Bridge")


@pytest.fixture
def failed_link_poll(monkeypatch):
    clock = SimpleNamespace(now=0)
    ports = {("s3", 2): "s3-right", ("s2", 2): "s2-right",
             ("s1", 2): "lost-left", ("s3", 1): "lost-right",
             ("s2", 1): "s2-left", ("s1", 1): "s1-left"}
    ids = ("8000.020000000001", "8000.020000000002", "8000.020000000003")
    bridges = {
        f"s{i}": {"stp_designated_root": ids[0], "stp_root_path_cost": str(cost),
                   "stp_bridge_id": bridge_id}
        for i, (cost, bridge_id) in enumerate(zip((0, 19, 38), ids), 1)
    }
    states = {"s1-left": ("designated", "forwarding"), "s2-left": ("root", "forwarding"),
              "lost-left": ("", "disabled"), "lost-right": ("", "disabled"),
              "s2-right": ("designated", "forwarding"), "s3-right": ("root", "forwarding")}
    observed = {name: {"stp_role": role, "stp_state": state} for name, (role, state) in states.items()}

    def advance(seconds):
        clock.now += seconds

    monkeypatch.setattr(harness, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=advance))
    monkeypatch.setattr(harness, "ovs_status", lambda table: bridges if table == "Bridge" else observed)
    return clock, ports


def test_failed_link_requires_physical_down_and_uninterrupted_match(monkeypatch, failed_link_poll):
    clock, ports = failed_link_poll

    def status(table, *fields):
        assert (table, fields) == ("Interface", ("admin_state", "link_state"))
        ready = clock.now not in {0, 1, 4}
        return {"lost-left": {"admin_state": "down", "link_state": "down"},
                "lost-right": {"admin_state": "down" if ready else "up", "link_state": "down"}}

    monkeypatch.setattr(harness, "ovs_rows", status)
    result = harness.verify("link_failure", CONFIG, ports, started=0)
    assert result["sampled_convergence_seconds"] == 5
    assert clock.now == 9
    assert result["time_basis"] == "wall_clock"
    assert set(result["failed_interfaces"]) == {"lost-left", "lost-right"}


def test_failed_link_cannot_pass_with_carrier_still_up(monkeypatch, failed_link_poll):
    _, ports = failed_link_poll
    monkeypatch.setattr(harness, "ovs_rows", lambda *_: {
        interface: {"admin_state": "down", "link_state": "up"}
        for interface in ("lost-right", "lost-left")
    })
    with pytest.raises(RuntimeError, match="within 90 s.*lost-left"):
        harness.verify("link_failure", CONFIG, ports, started=0)


def test_elapsed_timeout_reports_diagnostics_without_a_first_poll(failed_link_poll):
    clock, ports = failed_link_poll
    clock.now = 90
    with pytest.raises(RuntimeError, match="within 90 s"):
        harness.verify("link_failure", CONFIG, ports, started=0)


@pytest.mark.parametrize("success", [True, False])
def test_mininet_config_with_reordered_links_and_cleanup(monkeypatch, success):
    calls = []

    class FakeNetwork:
        def __init__(self, **kwargs):
            calls.append(("create", kwargs))

        def addSwitch(self, name, **kwargs):
            assert kwargs == {"failMode": "standalone", "stp": True}

        def addHost(self, name, **kwargs):
            assert kwargs["ip"] == f"10.0.0.{name[1:]}/24"

        def addLink(self, a, b, port1, port2=0):
            return SimpleNamespace(intf1=SimpleNamespace(name=f"{a}-port{port1}"),
                                   intf2=SimpleNamespace(name=f"{b}-port{port2}"))

        def start(self):
            calls.append(("start",))

        def configLinkStatus(self, *args):
            calls.append(("link", *args))

        def pingAll(self, timeout):
            assert timeout == "2"
            return 0 if success else 50

        def stop(self):
            calls.append(("stop",))

    monkeypatch.setitem(sys.modules, "mininet.net", SimpleNamespace(Mininet=FakeNetwork))
    monkeypatch.setitem(sys.modules, "mininet.node", SimpleNamespace(OVSSwitch="ovs"))
    monkeypatch.setattr(harness, "ovs", lambda *args: calls.append(("ovs", *args)))
    monkeypatch.setattr(harness, "ovs_status", lambda _: {
        f"s{i}-port3": {"stp_state": "forwarding"} for i in range(1, 4)
    })

    def verify(case, config, ports, started):
        calls.append(("verify", case))
        assert ports["s1", 1] == "s1-port1"
        assert ports["s3", 1] == "s3-port1"
        return {}

    monkeypatch.setattr(harness, "verify", verify)
    config = {**CONFIG, "links": list(reversed(CONFIG["links"]))}
    if success:
        assert harness.run("link_failure", config)["ping_loss_percent"] == 0
    else:
        with pytest.raises(RuntimeError, match="ping loss was 50%"):
            harness.run("link_failure", config)
    assert calls[0] == ("create", {"controller": None, "switch": "ovs"})
    assert calls[1] == ("start",)
    assert calls[2][:5] == ("ovs", "--", "set", "Port", "s2-port2")
    assert "other_config:stp-path-cost=19" in calls[2]
    assert "other_config:stp-priority=32768" in calls[2]
    assert "stp_enable=true" in calls[2] and "rstp_enable=false" in calls[2]
    assert calls[3:] == [("verify", "normal"), ("link", "s1", "s3", "down"),
                         ("verify", "link_failure"), ("stop",)]
