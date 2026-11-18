"""Validate classic STP against real OVS bridges on a dedicated Ubuntu VM."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "normal": ("s1", 32768, {"s1": 0, "s2": 19, "s3": 19}, [
        "designated", "root", "designated", "root", "designated", "alternate",
    ]),
    "priority_change": ("s3", 4096, {"s1": 19, "s2": 19, "s3": 0}, [
        "designated", "alternate", "root", "designated", "root", "designated",
    ]),
    "link_failure": ("s1", 32768, {"s1": 0, "s2": 19, "s3": 38}, [
        "designated", "root", "disabled", "disabled", "designated", "root",
    ]),
}


def ovs(*args):
    return subprocess.check_output(["ovs-vsctl", "--timeout=5", *args], text=True)


def ovs_status(table):
    payload = json.loads(ovs("--format=json", "--columns=name,status", "list", table))
    return {name: dict(status[1]) for name, status in payload["data"]}


def verify(case, config, ports, started):
    root, priority, costs, roles = EXPECTED[case]
    root_mac = config["bridges"][root]["mac"].replace(":", "")
    root_id = f"{priority:04x}.{root_mac}"
    states = [
        (role, {"alternate": "blocking", "disabled": "disabled"}.get(role, "forwarding"))
        for role in roles
    ]
    first_match = None
    while time.monotonic() - started < 90:
        bridge_status = ovs_status("Bridge")
        port_status = ovs_status("Port")
        observed = []
        for port in ports:
            status = port_status.get(port, {})
            state = status.get("stp_state")
            role = "disabled" if state == "disabled" else status.get("stp_role")
            observed.append((role, state))

        now = time.monotonic()
        ready = observed == states and all(
            bridge_status.get(name, {}).get("stp_designated_root") == root_id
            and bridge_status[name].get("stp_root_path_cost") == str(cost)
            for name, cost in costs.items()
        )
        if ready:
            if first_match is None:
                first_match = now
            if now - first_match >= 4:
                return {
                    "experiment": case,
                    "root_id": root_id,
                    "sampled_convergence_seconds": round(first_match - started, 3),
                    "bridges": {name: bridge_status[name] for name in costs},
                    "ports": {
                        port: {"role": role, "state": state}
                        for port, (role, state) in zip(ports, observed)
                    },
                }
        else:
            first_match = None
        time.sleep(1)
    raise RuntimeError(f"{case}: STP did not stabilize; bridges={bridge_status}, ports={observed}")


def run(case, config):
    from mininet.net import Mininet
    from mininet.node import OVSSwitch

    net = Mininet(controller=None, switch=OVSSwitch)
    try:
        for i, name in enumerate(config["bridges"], 1):
            net.addSwitch(name, failMode="standalone", stp=True)
            net.addHost(f"h{i}", ip=f"10.0.0.{i}/24")
            net.addLink(name, f"h{i}", port1=3)
        ports = []
        port_settings = []
        for _, left, left_port, right, right_port, cost in config["links"]:
            link = net.addLink(left, right, port1=left_port, port2=right_port)
            for intf, number in ((link.intf1, left_port), (link.intf2, right_port)):
                ports.append(intf.name)
                port_settings.extend(["--", "set", "Port", intf.name,
                                      f"other_config:stp-port-num={number}",
                                      f"other_config:stp-path-cost={cost}"])
        net.start()
        settings = []
        for name, bridge in config["bridges"].items():
            settings.extend(["--", "set", "Port", f"{name}-eth3", "other_config:stp-port-num=3",
                             "--", "set", "Bridge", name, "stp_enable=true", "rstp_enable=false",
                             f"other_config:stp-system-id={bridge['mac']}",
                             f"other_config:stp-priority={bridge['priority']}",
                             "other_config:stp-hello-time=2", "other_config:stp-max-age=20",
                             "other_config:stp-forward-delay=15"])
        started = time.monotonic()
        ovs(*port_settings, *settings)
        result = verify("normal", config, ports, started)
        if case != "normal":
            started = time.monotonic()
            if case == "priority_change":
                ovs("set", "Bridge", "s3", "other_config:stp-priority=4096")
            else:
                net.configLinkStatus("s1", "s3", "down")
            result = verify(case, config, ports, started)

        # Check host-facing ports too before exercising the learned data path.
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            status = ovs_status("Port")
            if all(status.get(f"{name}-eth3", {}).get("stp_state") == "forwarding"
                   for name in config["bridges"]):
                break
            time.sleep(1)
        else:
            raise RuntimeError("host-facing ports did not reach forwarding")
        # Mininet prints ping progress. Machine-readable output goes to --output.
        loss = net.pingAll(timeout="2")
        if loss != 0:
            raise RuntimeError(f"{case}: host ping loss was {loss}%")
        result["ping_loss_percent"] = loss
        return result
    finally:
        net.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=["all", *EXPECTED], nargs="?", default="all")
    parser.add_argument("--output", type=Path, default=Path("ovs-validation.json"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root on a dedicated VM with Mininet and OVS installed")
    config = json.loads((ROOT / "experiments" / "triangle.json").read_text())
    existing = set(ovs("list-br").splitlines()) & set(config["bridges"])
    if existing:
        parser.error(f"existing bridges would conflict with the lab: {sorted(existing)}")
    cases = EXPECTED if args.case == "all" else [args.case]
    results = [run(case, config) for case in cases]
    args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
