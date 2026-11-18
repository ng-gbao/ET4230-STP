# Technical audit and validation

## Audit findings

| Area | Evidence and disposition |
| --- | --- |
| Bridge IDs and input validation | On the baseline, `BridgeID(4096.0, "02:00:00:00:00:01")` produced `4096.0/02:00:00:00:00:01`. A link naming an unknown bridge raised a bare `KeyError`. Priority, port and cost fields now reject non-integers, including booleans. An unknown endpoint reports an invalid link by name. Regression tests cover these inputs. |
| Root election and port vectors | The simulator ranks the received root ID, total cost, sender Bridge ID, sender Port ID and local Port ID. The independent graph tests derive minimum costs and check the complete settled role and state at both ends of every link. They cover equal-cost sender and parallel-port ties, priority outranking MAC, and re-election within a partition. |
| BPDU lifecycle and event queue | Focused tests demonstrate periodic refresh and expiry, cancellation of stale in-flight BPDUs after a flap, and insertion order for tied events. The multi-change tests verify settled snapshots after link failures, recovery, priority restoration and partition rejoin. |
| Timing | The existing six experiment timelines and convergence values were reproduced. The exports now explicitly label simulated time and expose the observation-window requirement and end-to-end forwarding connectivity restoration. Tests re-run the graph at the reported event times and check a synthetic trace where connectivity restoration precedes the final port-state event. |
| OVSDB and interface mapping | The original parser raised `ValueError` on a mocked OVSDB table with reversed `status` and `name` headings. The original verification also depended on the traversal order of the configured links. The harness uses headings and logical bridge/port keys. Static tests cover reordering, malformed OVSDB maps and Mininet-provided interface names. |
| OVS link-down and polling | Mininet 2.3.0 logs interface configuration errors inside `configLinkStatus()`. The original harness only checked STP’s logical port state. It now verifies admin and carrier down on both failed interfaces, rejects a stale success after the deadline, includes observed state on timeout, and bounds each OVS command. Static tests include a mock disabled port with carrier still up and a poll that loses and regains its match. |

The Ubuntu review used Mininet’s published implementation and the OVS 3.3 schema. The schema documents dotted hexadecimal Bridge IDs, string-valued STP status maps and Interface admin and link state. [Mininet 2.3.0 source](https://github.com/mininet/mininet/blob/2.3.0/mininet/net.py) · [OVS 3.3 schema documentation](https://github.com/openvswitch/ovs/blob/v3.3.0/vswitchd/vswitch.xml)

## Independent oracle

The oracle lives in `tests/advanced/test_topologies.py`. Given the input graph and its down links, Floyd–Warshall finds distances within each component. The oracle selects the component’s lowest Bridge ID and restricts each non-root bridge’s parent candidates to links on a minimum-cost path. Among those it checks the documented sender and port tie-breakers. Each link’s designated endpoint is independently selected using its endpoint costs and Bridge IDs. The oracle checks selected parents, root IDs and costs, port roles, exact forwarding-edge sets, component reachability and a forest edge count of *bridges − components*. This graph computation runs only in the tests.

Eight fixed test configurations cover a single bridge, two bridges, triangle, square, chorded square, full mesh, weighted parallel links and disconnected components including an isolated bridge. Each of five scenario configurations is also run with seeds 4230–4234. Each seed uses an isolated Python `Random` instance, a base cycle, seeded additional edges and 4–8 bridges. Odd seeds use equal costs and even seeds sample from 4, 19 and 38. Tests allow at least one Max Age without a tracked state change before evaluating each checkpoint.

## Limits of the evidence

The oracle tests settled outcomes on small topologies. In between checkpoints the simulator can briefly use outdated BPDU information or have an incomplete or cyclic data graph. The finite advertisement lifetime, per-port cache, fixed propagation, shared timers and simultaneous endpoint link detection are simplifications. With an artificially high propagation delay or long path, relayed information can expire before reaching all bridges. There is no full convergence proof, malformed wire-frame handling or per-frame forwarding validation. The triangle experiment runner requires a connected final topology; disconnected components are checked directly in the simulator tests.

The Python and static harness tests ran in Cloud. The OVS status, timing and connectivity tests in the mock suite exercise the Python harness with representative data. The next source of real validation evidence is a VMware guest run with Mininet, OVS and a BPDU capture. [Ubuntu procedure](ubuntu.md)
