"""Discrete-event model of classic spanning tree on point-to-point links."""

from dataclasses import dataclass, field
from heapq import heappop, heappush
from itertools import count


@dataclass(frozen=True, order=True)
class BridgeID:
    priority: int
    mac: str

    def __post_init__(self):
        if (type(self.priority) is not int
                or not 0 <= self.priority <= 61440
                or self.priority % 4096):
            raise ValueError("priority must be a multiple of 4096 from 0 to 61440")
        octets = self.mac.split(":")
        if len(octets) != 6 or any(
            len(octet) != 2 or any(c not in "0123456789abcdefABCDEF" for c in octet)
            for octet in octets
        ):
            raise ValueError("expected a six-octet MAC address")
        object.__setattr__(self, "mac", self.mac.lower())

    def __str__(self):
        return f"{self.priority}/{self.mac}"


@dataclass(frozen=True)
class BPDU:
    root: BridgeID
    cost: int
    sender: BridgeID
    port: int
    expires: int

    @property
    def vector(self):
        return self.root, self.cost, self.sender, self.port


@dataclass
class Port:
    number: int
    cost: int
    peer: str
    peer_port: int
    up: bool = True
    received: BPDU | None = None
    role: str = "disabled"
    state: str = "disabled"
    transition: int = 0
    link_epoch: int = 0


@dataclass
class Bridge:
    identifier: BridgeID
    ports: dict[int, Port] = field(default_factory=dict)
    root: BridgeID = field(init=False)
    cost: int = 0
    root_port: int | None = None

    def __post_init__(self):
        self.root = self.identifier

    def vector(self, port):
        return self.root, self.cost, self.identifier, port


@dataclass(frozen=True)
class Timers:
    """Shared timer settings in milliseconds."""

    hello: int = 2000
    max_age: int = 20000
    forward_delay: int = 15000

    def __post_init__(self):
        limits = (
            (self.hello, 1000, 10000),
            (self.max_age, 6000, 40000),
            (self.forward_delay, 4000, 30000),
        )
        if any(type(value) is not int or not low <= value <= high for value, low, high in limits):
            raise ValueError("STP timers must be integer milliseconds within their valid ranges")
        if not 2 * (self.hello + 1000) <= self.max_age <= 2 * (self.forward_delay - 1000):
            raise ValueError("STP timers must satisfy the hello, max-age and forward-delay relationship")


class Simulation:
    def __init__(self, bridges, links, propagation=10, *, timers=Timers()):
        if type(propagation) is not int or not 0 < propagation < timers.hello:
            raise ValueError("propagation must be an integer between 0 and the hello interval")
        self.propagation = propagation
        self.timers = timers
        self.bridges = {
            name: Bridge(BridgeID(**identifier)) for name, identifier in bridges.items()
        }
        if not self.bridges or len({b.identifier.mac for b in self.bridges.values()}) != len(bridges):
            raise ValueError("topology must contain bridges with distinct MAC addresses")

        self.links = {}
        for name, a, a_port, b, b_port, cost in links:
            if (name in self.links or a == b or a not in self.bridges or b not in self.bridges
                    or type(cost) is not int or cost <= 0
                    or any(type(port) is not int or not 1 <= port <= 4095
                           for port in (a_port, b_port))
                    or a_port in self.bridges[a].ports or b_port in self.bridges[b].ports):
                raise ValueError(f"invalid link: {name}")
            left = self.bridges[a].ports[a_port] = Port(a_port, cost, b, b_port)
            right = self.bridges[b].ports[b_port] = Port(b_port, cost, a, a_port)
            self.links[name] = ((a, left), (b, right))

        self.now = 0
        self.sent = 0
        self.changes = []
        self._events = []
        self._sequence = count()
        for name in self.bridges:
            self.schedule(0, self._recompute, name)
        self.schedule(0, self._hello)

    @property
    def last_change(self):
        return self.changes[-1]["time_ms"] if self.changes else 0

    def _record(self, kind, **details):
        self.changes.append({"time_ms": self.now, "kind": kind, **details})

    def schedule(self, when, callback, *args):
        if type(when) is not int or when < self.now:
            raise ValueError("event time must be an integer at or after the current time")
        heappush(self._events, (when, next(self._sequence), callback, args))

    def run(self, until):
        if type(until) is not int or until < self.now:
            raise ValueError("simulation end must be an integer at or after the current time")
        while self._events and self._events[0][0] <= until:
            self.now, _, callback, args = heappop(self._events)
            callback(*args)
        self.now = until

    def _hello(self):
        for name in self.bridges:
            self._advertise(name)
        self.schedule(self.now + self.timers.hello, self._hello)

    def _advertise(self, name):
        bridge = self.bridges[name]
        expires = self.now + self.timers.max_age
        if bridge.root_port is not None:
            expires = bridge.ports[bridge.root_port].received.expires

        for port in bridge.ports.values():
            if port.role == "designated" and expires > self.now + self.propagation:
                bpdu = BPDU(*bridge.vector(port.number), expires)
                self.sent += 1
                self.schedule(self.now + self.propagation, self._receive,
                              port.peer, port.peer_port, port.link_epoch, bpdu)

    def _receive(self, name, number, epoch, bpdu):
        port = self.bridges[name].ports[number]
        if not port.up or epoch != port.link_epoch or bpdu.expires <= self.now:
            return
        if port.received == bpdu:
            return
        port.received = bpdu
        self.schedule(bpdu.expires, self._expire, name, port, bpdu)
        changed = self._recompute(name)
        if not changed and self.bridges[name].root_port == number:
            self._advertise(name)

    def _expire(self, name, port, bpdu):
        if port.received is bpdu:
            port.received = None
            self._recompute(name)

    def _recompute(self, name):
        bridge = self.bridges[name]
        previous = bridge.root, bridge.cost, bridge.root_port
        candidates = [(bridge.identifier, 0, bridge.identifier, 0, 0)]
        for port in bridge.ports.values():
            bpdu = port.received
            if port.up and bpdu and bpdu.expires > self.now:
                candidates.append((bpdu.root, bpdu.cost + port.cost,
                                   bpdu.sender, bpdu.port, port.number))
        root, cost, _, _, root_port = min(candidates)
        bridge.root, bridge.cost, bridge.root_port = root, cost, root_port or None
        changed = previous != (root, cost, bridge.root_port)
        if changed:
            self._record("root_selection", bridge=name, root_id=str(root),
                         root_cost=cost, root_port=bridge.root_port)

        for port in bridge.ports.values():
            if not port.up:
                role = "disabled"
            elif port.number == bridge.root_port:
                role = "root"
            elif (port.received and port.received.expires > self.now
                  and port.received.vector < bridge.vector(port.number)):
                role = "alternate"
            else:
                role = "designated"

            if role != port.role:
                changed = True
                was_active = port.role in {"designated", "root"}
                self._record("port_role", bridge=name, port=port.number,
                             previous=port.role, role=role)
                port.role = role
                if role in {"designated", "root"}:
                    if not was_active:
                        self._set_state(name, port, "listening")
                        self.schedule(self.now + self.timers.forward_delay, self._advance,
                                      name, port, port.transition, "learning")
                else:
                    self._set_state(name, port, "disabled" if role == "disabled" else "blocking")

        if changed:
            self._advertise(name)
        return changed

    def _set_state(self, name, port, state):
        self._record("port_state", bridge=name, port=port.number, previous=port.state, state=state)
        port.state = state
        port.transition += 1

    def _advance(self, name, port, transition, state):
        if port.transition != transition:
            return
        self._set_state(name, port, state)
        if state == "learning":
            self.schedule(self.now + self.timers.forward_delay, self._advance,
                          name, port, port.transition, "forwarding")

    def set_priority(self, name, priority):
        bridge = self.bridges[name]
        identifier = BridgeID(priority, bridge.identifier.mac)
        if identifier != bridge.identifier:
            self._record("bridge_id", bridge=name, previous=str(bridge.identifier),
                         bridge_id=str(identifier))
            bridge.identifier = identifier
            if not self._recompute(name):
                self._advertise(name)

    def set_link(self, name, up):
        endpoints = self.links[name]
        if endpoints[0][1].up == up:
            return
        self._record("link", link=name, up=up)
        for _, port in endpoints:
            port.up = up
            port.link_epoch += 1
            port.received = None
        for bridge, _ in endpoints:
            self._recompute(bridge)

    def snapshot(self):
        ports = [
            {"bridge": name, "port": port.number, "peer": port.peer,
             "peer_port": port.peer_port, "role": port.role, "state": port.state}
            for name, bridge in self.bridges.items()
            for port in bridge.ports.values()
        ]
        forwarding = {
            link: (a, b) for link, ((a, left), (b, right)) in self.links.items()
            if left.state == right.state == "forwarding"
        }
        components = connected_components(self.bridges, forwarding.values())

        return {
            "bridges": {
                name: {"bridge_id": str(bridge.identifier), "root_id": str(bridge.root),
                       "root_cost": bridge.cost, "root_port": bridge.root_port}
                for name, bridge in self.bridges.items()
            },
            "ports": ports,
            "state_counts": {
                state: sum(port["state"] == state for port in ports)
                for state in ("forwarding", "blocking", "disabled", "listening", "learning")
            },
            "forwarding_links": list(forwarding),
            "components": components,
            "connected": len(components) == 1,
            "loop_free": len(forwarding) == len(self.bridges) - len(components),
        }


def connected_components(bridges, edges):
    """Group bridge names by reachability in the supplied undirected graph."""
    graph = {name: set() for name in bridges}
    for a, b in edges:
        graph[a].add(b)
        graph[b].add(a)
    seen = set()
    components = []
    for name in graph:
        if name in seen:
            continue
        component = {name}
        pending = [name]
        while pending:
            node = pending.pop()
            for neighbor in graph[node] - component:
                component.add(neighbor)
                pending.append(neighbor)
        seen.update(component)
        components.append(sorted(component))
    return components
