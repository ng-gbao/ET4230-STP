import json
from pathlib import Path
import unittest

from stp.experiments import run
from stp.simulator import BridgeID, Simulation


CONFIG = json.loads((Path(__file__).parents[1] / "experiments/triangle.json").read_text())


def triangle(links=None):
    return Simulation(CONFIG["bridges"], CONFIG["links"] if links is None else links)


class SpanningTreeTests(unittest.TestCase):
    def test_bridge_id_priority_then_mac(self):
        self.assertLess(BridgeID(4096, "02:00:00:00:00:FF"),
                        BridgeID(8192, "02:00:00:00:00:01"))
        self.assertLess(BridgeID(4096, "02:00:00:00:00:01"),
                        BridgeID(4096, "02:00:00:00:00:FF"))
        with self.assertRaises(ValueError):
            BridgeID(1, "02:00:00:00:00:01")

    def test_bpdu_delay_and_forward_delay(self):
        sim = triangle()
        sim.run(9)
        self.assertEqual(sim.snapshot()["bridges"]["s3"]["root_port"], None)
        sim.run(10)
        self.assertEqual(sim.snapshot()["bridges"]["s3"]["root_port"], 1)
        sim.run(20)
        self.assertEqual(sim.snapshot()["state_counts"]["listening"], 5)
        self.assertFalse(sim.snapshot()["connected"])
        sim.run(15000)
        self.assertEqual(sim.snapshot()["state_counts"]["learning"], 5)
        sim.run(29999)
        self.assertFalse(sim.snapshot()["connected"])
        sim.run(30000)
        self.assertTrue(sim.snapshot()["connected"])
        self.assertTrue(sim.snapshot()["loop_free"])

    def test_parallel_links_break_tie_by_sender_port(self):
        sim = Simulation(
            {key: CONFIG["bridges"][key] for key in ("s1", "s2")},
            [("a", "s1", 2, "s2", 1, 19), ("b", "s1", 1, "s2", 2, 19)],
        )
        sim.run(60000)
        snapshot = sim.snapshot()
        self.assertEqual(snapshot["bridges"]["s2"]["root_port"], 2)
        self.assertEqual(snapshot["forwarding_links"], ["b"])
        self.assertEqual(snapshot["state_counts"]["blocking"], 1)

    def test_cost_beats_hop_count(self):
        links = [row if row[0] != "s1-s3" else [*row[:-1], 100]
                 for row in CONFIG["links"]]
        sim = triangle(links)
        sim.run(60000)
        snapshot = sim.snapshot()
        self.assertEqual(snapshot["bridges"]["s3"]["root_cost"], 38)
        self.assertEqual(snapshot["bridges"]["s3"]["root_port"], 2)
        self.assertEqual(snapshot["forwarding_links"], ["s1-s2", "s2-s3"])

    def test_partition_ages_out_old_root(self):
        sim = triangle()
        sim.run(60000)
        sim.set_link("s1-s2", False)
        sim.set_link("s1-s3", False)
        sim.run(120000)
        snapshot = sim.snapshot()
        self.assertEqual(snapshot["components"], [["s1"], ["s2", "s3"]])
        self.assertEqual(snapshot["bridges"]["s3"]["root_id"],
                         snapshot["bridges"]["s2"]["bridge_id"])
        self.assertEqual(snapshot["bridges"]["s3"]["root_cost"], 19)

    def test_in_flight_bpdu_discarded_after_link_flap(self):
        sim = triangle()
        sim.run(5)
        sim.set_link("s1-s3", False)
        sim.set_link("s1-s3", True)
        sim.run(10)
        self.assertIsNone(sim.bridges["s3"].ports[1].received)
        sim.run(15)
        self.assertEqual(sim.bridges["s3"].root_port, 1)

    def test_experiments_switch_expected_root_and_paths(self):
        results = {row["experiment"]: row for row in run(CONFIG)}
        normal, priority, failure = (results[name] for name in (
            "normal", "priority_change", "link_failure"
        ))
        self.assertEqual(normal["forwarding_links"], ["s1-s2", "s1-s3"])
        self.assertEqual(priority["forwarding_links"], ["s1-s3", "s2-s3"])
        self.assertEqual(failure["forwarding_links"], ["s1-s2", "s2-s3"])
        self.assertEqual(failure["bridges"]["s3"]["root_port"], 2)
        self.assertEqual(failure["bridges"]["s3"]["root_cost"], 38)
        self.assertEqual(priority["before"], failure["before"])


if __name__ == "__main__":
    unittest.main()
