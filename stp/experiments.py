"""Run reproducible experiment scenarios and export JSON and CSV."""

import argparse
import csv
from dataclasses import asdict
import json
from pathlib import Path

from stp.simulator import Simulation, Timers


def stable(sim, snapshot):
    counts = snapshot["state_counts"]
    return (snapshot["connected"] and snapshot["loop_free"]
            and not (counts["listening"] or counts["learning"])
            and sim.now - sim.last_change >= sim.timers.max_age)


def run(config):
    duration = config["phase_ms"]
    results = []
    for case in config["experiments"]:
        timers = Timers(**(config.get("timers", {}) | case.get("timers", {})))
        if duration < timers.max_age:
            raise ValueError("phase must accommodate the stability observation window")
        cost_overrides = case.get("path_costs", {})
        links = [
            (*row[:-1], cost_overrides.get(row[0], row[-1])) for row in config["links"]
        ]
        if cost_overrides.keys() - {row[0] for row in links}:
            raise ValueError(f"{case['name']}: path cost override names an unknown link")
        sim = Simulation(config["bridges"], links, config["propagation_ms"], timers=timers)
        trigger = 0
        first_change = 0
        before = None
        baseline_convergence_ms = None
        if "priority" in case or "fail_link" in case:
            sim.run(duration)
            before = sim.snapshot()
            if not stable(sim, before):
                raise RuntimeError(f"{case['name']}: baseline did not stabilize")
            baseline_convergence_ms = sim.last_change
            trigger = duration
            first_change = len(sim.changes)
            if "priority" in case:
                sim.set_priority(*case["priority"])
            if "fail_link" in case:
                sim.set_link(case["fail_link"], False)

        sim.run(trigger + duration)
        snapshot = sim.snapshot()
        root = snapshot["bridges"][case["expect_root"]]["bridge_id"]
        counts = snapshot["state_counts"]
        if (
            {b["root_id"] for b in snapshot["bridges"].values()} != {root}
            or not stable(sim, snapshot)
            or [counts[s] for s in ("forwarding", "blocking", "disabled")] != case["expect_counts"]
        ):
            raise RuntimeError(f"{case['name']}: unexpected or unstable outcome: {snapshot}")

        timeline = sim.changes[first_change:]
        control_ms = 0
        port_state_ms = 0
        for change in timeline:
            elapsed = change["time_ms"] - trigger
            if change["kind"] == "port_state":
                port_state_ms = elapsed
            elif change["kind"] != "link":
                control_ms = elapsed
        results.append({
            "experiment": case["name"],
            "phase": "reconvergence" if before is not None else "startup",
            "root_id": root,
            "trigger_ms": trigger,
            "observed_until_ms": sim.now,
            "baseline_convergence_ms": baseline_convergence_ms,
            "control_convergence_ms": control_ms,
            "port_state_convergence_ms": port_state_ms,
            "convergence_ms": max(control_ms, port_state_ms),
            "timers_ms": asdict(timers),
            "propagation_ms": sim.propagation,
            "path_costs": {row[0]: row[-1] for row in links},
            "timeline": timeline,
            "before": before,
            **snapshot,
        })
    return results


def export(results, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ("experiment", "phase", "root_id", "trigger_ms", "observed_until_ms",
                  "baseline_convergence_ms", "control_convergence_ms",
                  "port_state_convergence_ms", "convergence_ms", "forwarding", "blocking",
                  "disabled", "connected", "loop_free")
        writer = csv.DictWriter(stream, fields, lineterminator="\n")
        writer.writeheader()
        for result in results:
            writer.writerow({key: result.get(key, result["state_counts"].get(key)) for key in fields})

    with (output / "ports.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ("experiment", "bridge", "port", "peer", "peer_port", "role", "state")
        writer = csv.DictWriter(stream, fields, lineterminator="\n")
        writer.writeheader()
        for result in results:
            for port in result["ports"]:
                writer.writerow({"experiment": result["experiment"], **port})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("experiments/triangle.json"))
    parser.add_argument("--output", type=Path, default=Path("results"))
    args = parser.parse_args()
    results = run(json.loads(args.config.read_text(encoding="utf-8")))
    export(results, args.output)
    for result in results:
        print(f"{result['experiment']}: {result['root_id']}, "
              f"{result['convergence_ms']} ms, {result['state_counts']}")


if __name__ == "__main__":
    main()
