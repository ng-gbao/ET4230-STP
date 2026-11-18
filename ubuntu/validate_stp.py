"""Validate classic STP against real OVS bridges on a dedicated Ubuntu VM."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "normal": ("s1", 32768, {"s1": 0, "s2": 19, "s3": 19}, {
        "s1": ("designated", "designated"), "s2": ("root", "designated"),
        "s3": ("root", "alternate"),
    }),
    "priority_change": ("s3", 4096, {"s1": 19, "s2": 19, "s3": 0}, {
        "s1": ("designated", "root"), "s2": ("alternate", "root"),
        "s3": ("designated", "designated"),
    }),
    "link_failure": ("s1", 32768, {"s1": 0, "s2": 19, "s3": 38}, {
        "s1": ("designated", "disabled"), "s2": ("root", "designated"),
        "s3": ("disabled", "root"),
    }),
}


def ovs(*args):
    command = ["ovs-vsctl", "--timeout=5", *args]
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True, timeout=7).stdout
    except OSError as error:
        raise RuntimeError(f"could not run ovs-vsctl: {error}") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"ovs-vsctl returned {error.returncode}: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"ovs-vsctl exceeded its process timeout: {args}") from error


def ovs_rows(table, *columns):
    selection = ",".join(("name", *columns))
    try:
        payload = json.loads(ovs("--format=json", f"--columns={selection}", "list", table))
        headings = payload["headings"]
        if set(headings) != {"name", *columns} or len(headings) != len(columns) + 1:
            raise ValueError(f"unexpected headings {headings}")
        rows = (dict(zip(headings, values, strict=True)) for values in payload["data"])
        return {row.pop("name"): row for row in rows}
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"invalid OVSDB {table} output: {error}") from error


def ovs_status(table):
    statuses = {}
    for name, row in ovs_rows(table, "status").items():
        try:
            tag, data = row["status"]
            if tag != "map":
                raise ValueError(f"expected OVSDB map, received {tag}")
            statuses[name] = dict(data)
        except (ValueError, TypeError) as error:
            raise RuntimeError(f"invalid OVSDB status for {table} {name}: {error}") from error
    return statuses


def verify(case, config, ports, started):
    root, priority, costs, roles = EXPECTED[case]
    bridge_ids = {}
    for name, bridge in config["bridges"].items():
        bridge_priority = priority if name == root else bridge["priority"]
        mac = bridge["mac"].replace(":", "").lower()
        bridge_ids[name] = f"{bridge_priority:04x}.{mac}"
    root_id = bridge_ids[root]
    expected = {}
    for (bridge, number), interface in ports.items():
        role = roles[bridge][number - 1]
        state = {"alternate": "blocking", "disabled": "disabled"}.get(role, "forwarding")
        expected[interface] = (role, state)
    failed = {interface for interface, (_, state) in expected.items() if state == "disabled"}
    first_match = None
    bridge_status, observed, interface_status = {}, {}, {}
    deadline = started + 90
    while time.monotonic() < deadline:
        bridge_status = ovs_status("Bridge")
        port_status = ovs_status("Port")
        if failed:
            interface_status = ovs_rows("Interface", "admin_state", "link_state")
        for port in expected:
            status = port_status.get(port, {})
            state = status.get("stp_state")
            role = "disabled" if state == "disabled" else status.get("stp_role")
            observed[port] = (role, state)

        now = time.monotonic()
        if now >= deadline:
            break
        ready = observed == expected and all(
            interface_status.get(port) == {"admin_state": "down", "link_state": "down"}
            for port in failed
        ) and all(
            bridge_status.get(name, {}).get("stp_designated_root", "").lower() == root_id
            and bridge_status[name].get("stp_bridge_id", "").lower() == bridge_ids[name]
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
                    "time_basis": "wall_clock",
                    "sampled_convergence_seconds": round(first_match - started, 3),
                    "bridges": {name: bridge_status[name] for name in costs},
                    "ports": {
                        port: {"role": role, "state": state}
                        for port, (role, state) in observed.items()
                    },
                    "failed_interfaces": {port: interface_status[port] for port in sorted(failed)},
                }
        else:
            first_match = None
        time.sleep(1)
    failed_status = {port: interface_status.get(port) for port in sorted(failed)}
    raise RuntimeError(f"{case}: STP did not stabilize within 90 s. "
                       f"Expected root={root_id}, costs={costs}, ports={expected}. "
                       f"Observed bridges={bridge_status}, ports={observed}, "
                       f"failed interfaces={failed_status}")


def run(case, config):
    from mininet.net import Mininet
    from mininet.node import OVSSwitch

    net = Mininet(controller=None, switch=OVSSwitch)
    try:
        print(f"{case}: building Mininet topology and waiting for a stable baseline", file=sys.stderr)
        host_interfaces = {}
        for i, name in enumerate(config["bridges"], 1):
            net.addSwitch(name, failMode="standalone", stp=True)
            net.addHost(f"h{i}", ip=f"10.0.0.{i}/24")
            link = net.addLink(name, f"h{i}", port1=3)
            host_interfaces[name] = link.intf1.name
        ports = {}
        port_settings = []
        for _, left, left_port, right, right_port, cost in config["links"]:
            link = net.addLink(left, right, port1=left_port, port2=right_port)
            for bridge, intf, number in ((left, link.intf1, left_port),
                                         (right, link.intf2, right_port)):
                ports[bridge, number] = intf.name
                port_settings.extend(["--", "set", "Port", intf.name,
                                      f"other_config:stp-port-num={number}",
                                      f"other_config:stp-path-cost={cost}"])
        settings = []
        for name, bridge in config["bridges"].items():
            settings.extend(["--", "set", "Port", host_interfaces[name], "other_config:stp-port-num=3",
                             "--", "set", "Bridge", name, "stp_enable=true", "rstp_enable=false",
                             f"other_config:stp-system-id={bridge['mac']}",
                             f"other_config:stp-priority={bridge['priority']}",
                             "other_config:stp-hello-time=2", "other_config:stp-max-age=20",
                             "other_config:stp-forward-delay=15"])
        started = time.monotonic()
        net.start()
        ovs(*port_settings, *settings)
        result = verify("normal", config, ports, started)
        if case != "normal":
            print(f"{case}: applying scenario change", file=sys.stderr)
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
            if all(status.get(name, {}).get("stp_state") == "forwarding"
                   for name in host_interfaces.values()):
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
    try:
        existing = set(ovs("list-br").splitlines()) & set(config["bridges"])
        if existing:
            parser.error(f"existing bridges would conflict with the lab: {sorted(existing)}")
        cases = EXPECTED if args.case == "all" else [args.case]
        results = [run(case, config) for case in cases]
        args.output.write_text(json.dumps(results, indent=2) + "\n")
    except (RuntimeError, ImportError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
