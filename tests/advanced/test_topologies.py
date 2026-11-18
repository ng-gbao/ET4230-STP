"""Compare settled BPDU decisions to an independent shortest-path oracle."""

from collections import defaultdict
from itertools import combinations
from math import inf
from random import Random

import pytest

from stp.simulator import Simulation


def topology(size, edges):
    bridges = {
        f"s{i}": {"priority": 32768, "mac": f"02:00:00:00:00:{i:02x}"}
        for i in range(1, size + 1)
    }
    ports = defaultdict(int)
    links = []
    for index, (a, b, cost) in enumerate(edges):
        ports[a] += 1
        ports[b] += 1
        links.append((f"e{index}", f"s{a}", ports[a], f"s{b}", ports[b], cost))
    return bridges, links


def assert_final_state(sim, bridges, links, down=()):
    """Find shortest paths by Floyd–Warshall, then check every settled port."""
    ids = {name: (bridge["priority"], int(bridge["mac"].replace(":", ""), 16))
           for name, bridge in bridges.items()}
    distances = {(a, b): 0 if a == b else inf for a in bridges for b in bridges}
    for edge, a, _, b, _, cost in links:
        if edge not in down:
            distances[a, b] = distances[b, a] = min(cost, distances[a, b])
    for via in bridges:
        for a in bridges:
            for b in bridges:
                distances[a, b] = min(distances[a, b],
                                      distances[a, via] + distances[via, b])

    root = {a: min((b for b in bridges if distances[a, b] < inf), key=ids.get)
            for a in bridges}
    components = {frozenset(a for a in bridges if root[a] == r) for r in root.values()}
    costs = {a: distances[a, root[a]] for a in bridges}

    # A link qualifies only if it leads down an independently computed minimum-cost path.
    parents = defaultdict(list)
    for edge, a, ap, b, bp, cost in links:
        if edge in down:
            continue
        if costs[a] == costs[b] + cost:
            parents[a].append((ids[b], bp, ap))
        if costs[b] == costs[a] + cost:
            parents[b].append((ids[a], ap, bp))
    root_ports = {a: min(parents[a])[2] if a != root[a] else None for a in bridges}

    snapshot = sim.snapshot()
    for bridge, report in snapshot["bridges"].items():
        winner = bridges[root[bridge]]
        assert report["root_id"] == f"{winner['priority']}/{winner['mac']}"
        assert report["root_cost"] == costs[bridge]
        assert report["root_port"] == root_ports[bridge]

    observed = {(row["bridge"], row["port"]): (row["role"], row["state"])
                for row in snapshot["ports"]}
    forwarding = set()
    for edge, a, ap, b, bp, _ in links:
        if edge in down:
            assert observed[a, ap] == observed[b, bp] == ("disabled", "disabled")
            continue
        endpoints = sorted(((a, ap), (b, bp)), key=lambda end: (costs[end[0]], ids[end[0]]))
        designated, other = endpoints
        assert observed[designated] == ("designated", "forwarding")
        if root_ports[other[0]] == other[1]:
            assert observed[other] == ("root", "forwarding")
            forwarding.add(edge)
        else:
            assert observed[other] == ("alternate", "blocking")

    assert set(snapshot["forwarding_links"]) == forwarding
    assert {frozenset(group) for group in snapshot["components"]} == components
    assert snapshot["connected"] == (len(components) == 1)
    assert snapshot["loop_free"]
    assert len(forwarding) == len(bridges) - len(components)
    assert sim.now - sim.last_change >= sim.timers.max_age
    return forwarding


@pytest.mark.parametrize(("size", "edges"), [
    (1, []),
    (2, [(1, 2, 4)]),
    (3, [(1, 2, 19), (2, 3, 19), (3, 1, 19)]),
    (4, [(1, 2, 19), (2, 3, 19), (3, 4, 19), (4, 1, 19)]),
    (4, [(1, 2, 4), (1, 3, 10), (2, 3, 4), (3, 4, 4), (4, 1, 10)]),
    (5, [(a, b, (a + b) % 3 + 1) for a, b in combinations(range(1, 6), 2)]),
    (3, [(1, 2, 5), (1, 2, 5), (1, 2, 20), (2, 3, 5)]),
    (6, [(1, 2, 4), (2, 3, 4), (1, 3, 19), (4, 5, 4)]),
], ids=["single", "pair", "triangle", "square", "chorded-square", "mesh",
        "parallel", "disconnected"])
def test_final_state_for_named_topologies(size, edges):
    bridges, links = topology(size, edges)
    if size >= 3:
        bridges["s3"]["priority"] = 4096
    sim = Simulation(bridges, links)
    sim.run(90000)
    assert_final_state(sim, bridges, links)


SEEDS = range(4230, 4235)


def seeded_topology(seed):
    rng = Random(seed)
    size = 4 + seed - SEEDS.start
    cycle = [(n, n + 1) for n in range(1, size)] + [(1, size)]
    shortcuts = sorted(set(combinations(range(1, size + 1), 2)) - set(cycle))
    rng.shuffle(shortcuts)
    pairs = cycle + shortcuts[:size // 2]
    rng.shuffle(pairs)
    bridges, links = topology(size, [(a, b, 19 if seed % 2 else rng.choice((4, 19, 38)))
                                     for a, b in pairs])
    return bridges, links


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("scenario", [
    "startup", "priority-cycle", "forwarding-failure", "blocked-failure", "flaps-partition",
])
def test_seeded_topologies_against_reference(seed, scenario):
    bridges, links = seeded_topology(seed)
    sim = Simulation(bridges, links)
    sim.run(90000)
    active = assert_final_state(sim, bridges, links)
    if scenario == "startup":
        return

    if scenario == "priority-cycle":
        new_root = next(reversed(bridges))
        sim.set_priority(new_root, 4096)
        bridges[new_root]["priority"] = 4096
        sim.run(180000)
        assert_final_state(sim, bridges, links)
        sim.set_priority(new_root, 32768)
        bridges[new_root]["priority"] = 32768
    elif scenario in {"forwarding-failure", "blocked-failure"}:
        edge = next(name for name, *_ in links if (name in active) == (scenario == "forwarding-failure"))
        sim.set_link(edge, False)
        sim.run(180000)
        assert_final_state(sim, bridges, links, down={edge})
        sim.set_link(edge, True)
    else:
        adjacent = [edge for edge, a, _, b, _, _ in links if "s1" in (a, b)]
        flapping = adjacent[0]
        for offset, up in enumerate((False, True, False, True), 1):
            sim.schedule(90000 + offset, sim.set_link, flapping, up)
        for edge in adjacent:
            sim.schedule(92000, sim.set_link, edge, False)
        sim.run(180000)
        assert_final_state(sim, bridges, links, down=set(adjacent))
        for edge in adjacent:
            sim.set_link(edge, True)

    sim.run(270000)
    assert assert_final_state(sim, bridges, links) == active
