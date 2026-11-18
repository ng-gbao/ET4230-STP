# ET4230 – Spanning Tree Protocol

A distributed, discrete-event simulation of classic IEEE 802.1D Spanning Tree Protocol (STP). The Python model exchanges configuration BPDUs between bridges and tracks convergence in simulated milliseconds. A separate Mininet and Open vSwitch lab exercises classic STP on Ubuntu 24.04 LTS.

## Installation and reproduction

Use Python 3.10 or newer. Running the simulator, exporting CSV/JSON, and generating the SVG require only the standard library. The development requirements add pytest and CairoSVG. PNG rendering also needs the system Cairo library and the DejaVu Sans font. On Debian or Ubuntu, these are available as `libcairo2` and `fonts-dejavu-core`.

From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -ra
python -m stp.experiments --config experiments/triangle.json --output results
python -m stp.plot --input results/summary.json --output results/comparison.svg --png plots/comparison.png
```

Omit `--png` to run the SVG generator without CairoSVG. To verify reproduction without overwriting the supplied files:

```sh
repro=$(mktemp -d)
python -m stp.experiments --config experiments/triangle.json --output "$repro"
python -m stp.plot --input "$repro/summary.json" --output "$repro/comparison.svg" --png "$repro/comparison.png"
for artifact in summary.json summary.csv ports.csv comparison.svg; do
    cmp "results/$artifact" "$repro/$artifact" || exit 1
done
cmp plots/comparison.png "$repro/comparison.png"
```

The same version of Cairo and the same font installation are needed for a byte-for-byte PNG comparison. The CSV, JSON, and SVG do not depend on the rasterizer. [Cloud environment and verification details](CLOUD.md)

GitHub Actions runs the same pytest suite on Python 3.11 and 3.12 with the pinned dependencies in `requirements-ci.txt`. It compiles the Python sources and compares regenerated JSON, CSV and SVG files with their committed references. The workflow uploads JUnit XML and rebuilt artifacts. [CI workflow](.github/workflows/ci.yml)

## How the simulation works

- `stp/simulator.py` maintains independent bridge and port state. A priority queue delivers BPDUs and timer expirations in timestamp order, breaking equal-time ties by insertion order. There is no global spanning-tree computation.
- Every bridge starts with its own Bridge ID as root. Bridge IDs compare by priority, then numerically by MAC address. Root Port candidates compare by advertised root ID, advertised root path cost plus local ingress cost, sender Bridge ID, sender port number, then local port number.
- Each remaining active port compares its own outgoing vector to the most recent live vector received from its peer. A lower outgoing vector wins Designated Port. A losing non-root port becomes alternate and blocks.
- Designated ports transmit configuration BPDUs at the hello interval and on relevant changes. BPDUs take a configured time to arrive, and the received vector expires if it stops being refreshed. Root and Designated ports entering the active set wait through listening and learning before forwarding.
- Link-down events disable both ends, discard stored BPDUs on those ports and invalidate BPDUs already in flight. A restored link starts with fresh port state and timers. The forwarding graph includes a link when both of its endpoints forward, and checks that the graph is connected and acyclic, counting parallel links separately.

### Simplifications relative to IEEE 802.1D

This is an instructional model of classic STP. It uses point-to-point Ethernet links, symmetric integer path costs, fixed link delay, deterministic event ordering, a common port priority, and a zero system-ID extension. It restricts bridge priorities to multiples of 4096. It represents BPDUs as in-memory records and stores the most recent valid advertisement per peer. Designated ports send periodic and triggered advertisements, and relays inherit a fixed expiry timestamp from the originating root. This approximates, rather than implements, the wire protocol's message-age, timer advertisement, superior/inferior reception and transmit hold rules. The model assumes one shared timer configuration and instantaneous, simultaneous local link-state detection in both directions.

The listening and learning states represent timers. Frame forwarding, MAC learning and aging, TCN/TCA messages, VLANs, shared LAN segments, and RSTP are outside the model. Connectivity is a graph calculation. The tests assert final loop freedom and examine selected transitions. They do not establish transient loop freedom for arbitrary topologies or full IEEE conformance.

## Verification methodology

The pytest suite checks bridge and port elections, tie-breaking, BPDU expiry, equal-time event ordering, port transitions and topology changes. It covers isolated and disconnected bridges, pairs, triangles, square and chorded rings, a complete mesh and parallel links. A test-only shortest-path oracle independently computes the minimum Bridge ID per physical component, root costs, eligible Root Ports and Designated Port winners. The tests compare all settled bridge and port decisions and require exactly one forwarding tree per component.

Seeds 4230 through 4234 build rings of four through eight bridges with seeded extra links and a mix of equal and unequal costs. Each seed exercises startup, priority reduction and restoration, forwarding-link failure, blocked-link failure, and a sequence of flaps, partition and rejoin. Every settled checkpoint is compared with the oracle. Pytest also compares output schemas and tests the Ubuntu harness with mocked Mininet and OVSDB. [Technical audit](docs/validation.md)

## Experiments

[The triangle configuration](experiments/triangle.json) defines three bridges, a redundant link, default path cost 19 and one-way BPDU latency 10 ms. The default Hello Time, Max Age and Forward Delay are 2, 20 and 15 seconds respectively. A startup phase runs for 90 simulated seconds. Each change case starts from a fresh, independently converged baseline, applies its change at 90 seconds, and observes 90 more seconds.

| Scenario | Root | Forwarding | Blocking | Disabled | Control stable (sim. s) | Overall convergence (sim. s) | Connectivity restored (sim. s) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Normal triangle | s1 | 5 | 1 | 0 | 0.020 | 30.000 | 30.000 |
| Lower s3 priority to 4096 | s3 | 5 | 1 | 0 | 0.020 | 30.000 | 30.000 |
| Fail forwarding link s1–s3 | s1 | 4 | 0 | 2 | 0.000 | 30.000 | 30.000 |
| Fail non-forwarding link s2–s3 | s1 | 4 | 0 | 2 | 0.000 | 0.000 | 0.000 |
| Raise path cost on s1–s3 to 100 | s1 | 5 | 1 | 0 | 0.020 | 30.000 | 30.000 |
| Shorten Forward Delay to 12 s | s1 | 5 | 1 | 0 | 0.020 | 24.000 | 24.000 |

All six final forwarding graphs are connected and loop-free. The default Root Bridge ID is `32768/02:00:00:00:00:01`. The priority experiment elects `4096/02:00:00:00:00:03`. The higher-cost experiment sends s3 through s2 at total cost 38, changing the forwarding path while leaving the convergence duration unchanged.

### Why three cases reported 30 seconds

The clock starts at simulation time zero for startup or at the configured change for reconvergence. All exported durations use simulated milliseconds.

- **Control-plane convergence** is the last bridge-ID, root-selection, path-cost or port-role change timestamp minus the trigger timestamp. **Port-state convergence** uses the last port-state transition. An event category with no post-trigger change contributes zero.
- **Overall convergence** is the maximum of the two. The runner validates a final connected, loop-free graph and requires that it has observed at least one configured Max Age since the last recorded change. This stability window is excluded from the duration. BPDU refreshes with unchanged decisions do not restart the clock.
- **Connectivity restoration** is the start of the final uninterrupted interval with a connected forwarding graph, minus the trigger timestamp. The measurement evaluates both ends of each link and groups all port updates at the same simulated timestamp. A scenario with continuously available connectivity reports zero. This is a reachability measure, so it can differ from overall convergence and does not establish transient loop freedom.

In the baseline, the surviving eligible ports enter listening at 0 ms, learning at 15,000 ms and forwarding at 30,000 ms. During both priority reconvergence and failure of the forwarding link, the previously blocked s3 port 2 enters listening at absolute time 90,000 ms, learning at 105,000 ms and forwarding at 120,000 ms. The control plane settles within 20 ms of the trigger in all three cases, leaving the two Forward Delay intervals as the limiting factor. The shorter timer reduces the measured duration to 24 seconds. Removing the s2–s3 link disables its endpoints at the trigger while the forwarding graph stays intact, so no delay is needed. Zero here means there was no later modeled transition.

[Summary JSON with event timeline and baseline snapshots](results/summary.json) · [Summary CSV](results/summary.csv) · [Per-port CSV](results/ports.csv) · [SVG plot](results/comparison.svg) · [PNG plot](plots/comparison.png)

## Ubuntu 24.04 LTS validation

The separate harness provisions Mininet hosts and real OVS bridges, configures STP priority and port path costs, and checks elected root, port roles and states, both ends of the failed link, and end-to-end ping. It supports normal, priority-change and link-failure cases. Syntax, CLI and the mock harness tests ran in Codex Cloud. Real data-plane validation remains pending on an Ubuntu VM. [Run the VMware lab, inspect OVS, and capture BPDUs](docs/ubuntu.md)
