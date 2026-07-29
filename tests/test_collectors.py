import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mojatools_agent as agent


class CpuParsingTest(unittest.TestCase):
    def test_parse_cpu_times(self):
        total, idle = agent._parse_cpu_times("cpu  100 0 50 200 50 0 0 0 0 0")
        self.assertEqual(total, 400)
        self.assertEqual(idle, 250)

    def test_cpu_percent(self):
        # dt=400, di=250 -> busy 150/400 = 37.5%
        self.assertEqual(agent._cpu_percent(400, 250, 800, 500), 37.5)
        self.assertIsNone(agent._cpu_percent(400, 250, 400, 250))  # no delta


class MemoryTest(unittest.TestCase):
    def test_collect_memory(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "meminfo"), "w") as f:
                f.write("MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n")
            percent, total_mb = agent.collect_memory(d)
        self.assertEqual(percent, 50.0)
        self.assertEqual(total_mb, 16000)


class PortsTest(unittest.TestCase):
    TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1 0000000000000000 100 0 0 10 0
   1: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12346 1 0000000000000000 100 0 0 10 0
   2: 00000000:0050 00000000:0000 01 00000000:00000000 00:00000000 00000000     0        0 12347 1 0000000000000000 100 0 0 10 0
"""

    def test_only_listen_ports(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "net"))
            with open(os.path.join(d, "net", "tcp"), "w") as f:
                f.write(self.TCP)
            ports = agent.collect_listening_ports(d)
        self.assertEqual(ports, [22, 631])  # 0x16=22, 0x277=631; 0x50 state 01 excluded


class DisksTest(unittest.TestCase):
    def test_pseudo_fs_filtered(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "mounts"), "w") as f:
                f.write("tmpfs /run tmpfs rw 0 0\n")
                f.write("/dev/sda1 / ext4 rw 0 0\n")
                f.write("proc /proc proc rw 0 0\n")
            disks = agent.collect_disks(d)
        mounts = [x["mount"] for x in disks]
        self.assertEqual(mounts, ["/"])  # only the real fs on / survives


if __name__ == "__main__":
    unittest.main()
