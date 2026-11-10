import unittest
from datapack.domain import time_seconds
class TimeTests(unittest.TestCase):
    def test_offset(self):
        self.assertEqual(time_seconds('2026-11-01T08:00:00+08:00'),time_seconds('2026-11-01T00:00:00Z'))
    def test_naive_rejected(self):
        with self.assertRaises(ValueError):time_seconds('2026-11-01T00:00:00')
