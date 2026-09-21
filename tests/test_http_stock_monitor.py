import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from http_stock_monitor import parse_rfchost, parse_zgocloud


class ParserTest(unittest.TestCase):
    def test_zgocloud_out_of_stock(self):
        html = '<h1>Tokyo Intel VPS</h1><div>Starter</div><button>Out of stock!</button><div>Standard</div>'
        self.assertEqual(parse_zgocloud(html), ('unavailable', 0))

    def test_zgocloud_available(self):
        html = '<h1>Tokyo Intel VPS</h1><div>Starter</div><button>Continue</button><div>Standard</div>'
        self.assertEqual(parse_zgocloud(html), ('available', None))

    def test_rfchost_stock_count(self):
        html = '<h3>JP2-CO-Micro-Lite</h3><div>2 Available</div><h3>JP2-CO-Mini-Lite</h3>'
        self.assertEqual(parse_rfchost(html), ('available', 2))

    def test_rfchost_zero_stock(self):
        html = '<h3>JP2-CO-Micro-Lite</h3><div>0 Available</div><h3>JP2-CO-Mini-Lite</h3>'
        self.assertEqual(parse_rfchost(html), ('unavailable', 0))


if __name__ == '__main__':
    unittest.main()
