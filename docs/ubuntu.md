# Ubuntu 24.04 LTS Mininet and Open vSwitch lab

Use a dedicated VM with administrative access and network namespace and veth support. The lab creates OVS bridges named `s1`, `s2` and `s3` and stops the network on exit. Its validation output describes a real OVS run, independent of the Python simulation artifacts.

## Install and run

Enable the Ubuntu Universe repository if `mininet` is unavailable from APT. From the repository root:

```sh
sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch net-tools iputils-ping tcpdump
sudo systemctl enable --now openvswitch-switch
sudo ovs-vsctl show
sudo /usr/bin/python3 ubuntu/validate_stp.py all --output /tmp/et4230-ovs-validation.json
```

Choose `normal`, `priority_change` or `link_failure` in place of `all` to run a single case. The harness takes the topology and costs from `experiments/triangle.json`. It enables classic STP, disables RSTP, sets bridge priorities and port costs, and attaches one host per bridge. For the priority or link change it first verifies the baseline, then lowers the priority of s3 or brings down both ends of s1–s3. It requires the expected root ID and path cost on every bridge and the expected inter-switch port roles and states for at least four seconds before checking ping between hosts. It then writes the JSON file with the sampled wall-clock convergence and ping loss. A run of multiple scenarios writes results after every selected scenario passes.

To inspect the device while a scenario runs:

```sh
sudo ovs-vsctl --columns=name,stp_enable,rstp_enable,status list Bridge
sudo ovs-vsctl --columns=name,other_config,status list Port
sudo ovs-vsctl get Bridge s1 status:stp_designated_root
sudo ovs-vsctl get Port s3-eth2 status:stp_state
```

The expected OVS root IDs are `8000.020000000001` in the normal and failed-link cases, and `1000.020000000003` after the priority change. OVS prints the priority portion in hexadecimal. For active inter-switch ports, `status:stp_role` reports the role and `status:stp_state` reports the forwarding state. Normal OVS timing can differ from the simulator's event timing and clock definition.

## Capture BPDUs

In a second VM terminal, wait for the inter-switch interface and start capture while the lab is running:

```sh
until ip link show dev s1-eth1 >/dev/null 2>&1; do sleep 0.25; done
sudo tcpdump -i s1-eth1 -s 0 -U -w /tmp/et4230-stp.pcap 'ether dst 01:80:c2:00:00:00'
```

For this capture run one scenario at a time, for example `link_failure`. The s1–s2 link carrying `s1-eth1` stays up when s1–s3 is disabled. Stop tcpdump with Ctrl+C before starting another scenario, as Mininet recreates interfaces. To inspect the capture:

```sh
sudo tcpdump -nn -e -vv -r /tmp/et4230-stp.pcap
```

In Wireshark, open the capture and apply the display filter `stp`. Inspect Root Identifier, Root Path Cost, Bridge Identifier, Port Identifier and timer values, and compare them with the OVS status observed during the same run.
