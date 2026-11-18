# Codex Cloud verification

The workspace was checked on 18 November 2026. It reported Debian 13.6 (trixie), Linux 6.18.57 and Python 3.12.14. Python 3.11.18 was also provisioned in a temporary directory for the verification matrix.

| Probe | Observed result |
| --- | --- |
| `id` and `/proc/self/status` | The normal shell ran as UID/GID 1000 (`agent`) with empty effective and permitted capability sets, including no effective `CAP_NET_ADMIN`. Seccomp filter mode was 2. |
| `unshare --net true` | Failed with `Operation not permitted`. |
| `unshare --user --map-root-user --net true` | Succeeded in the published workspace. This checks creation of a private user and network namespace. |
| `command -v ip tcpdump mn ovs-vsctl ovs-vswitchd` | Only `ip` was found. |
| `/dev/net/tun` | Device path present in the published workspace. |

The ordinary Cloud shell lacked the privileges and tooling for the documented system Mininet/OVS workflow. The system OVS lab and BPDU capture were not run in Cloud or on a separate Ubuntu VM. [Ubuntu VMware runtime instructions](docs/ubuntu.md)

## Checks executed in Codex Cloud

- pytest 8.4.2 reported **78 passed, 0 failed, 0 skipped** on each of Python 3.11 and 3.12, including topology oracle and mock OVS/Mininet tests.
- All six Python experiments passed final root election, port counts, connectivity, cycle detection and stability assertions. The existing timelines and convergence values match the baseline.
- Regenerated CSV, JSON and SVG matched the committed references on both Python versions. PNG also reproduced byte for byte with CairoSVG 2.8.2, Debian Cairo 1.18.4 and DejaVu Sans 2.37 on this machine.
- The Ubuntu harness compiled and displayed its CLI help with system Python. Actionlint 1.7.7 validated the GitHub Actions workflow locally.
