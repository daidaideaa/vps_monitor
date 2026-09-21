import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import publish_status
from export_status import combined_snapshot
from stock_targets import TARGETS
from vps_stock_monitor import update_product


class JapanTest(unittest.TestCase):
    def test_direct_japan_keeps_episode_and_notification_flags(self):
        old = {**TARGETS[0], 'last_confirmed':'unavailable', 'status':'unavailable',
               'last_checked':'2026-09-21T00:00:00Z', 'unavailable_since':'2026-09-20T00:00:00Z',
               'error_notified':True, 'query_location':'hong-kong-vps'}
        with patch.dict(os.environ, QUERY_LOCATION='japan-home-vps'):
            new = update_product(TARGETS[0], {'status':'unknown'}, old, '2026-09-22T00:00:00Z')
            public = combined_snapshot({}, {'products':[new]})
        self.assertEqual(new['query_location'], 'japan-home-vps')
        self.assertEqual(new['unavailable_since'], old['unavailable_since'])
        self.assertTrue(new['error_notified'])
        self.assertEqual(public['query_location'], 'japan-home-vps')
        self.assertEqual(public['products'][1]['query_location'], 'japan-home-vps')

    def test_publish_failure_retries_and_unchanged_snapshot_is_not_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'publisher.env';config.write_text('STATUS_PUBLISH_URL=https://example.invalid/publish\nSTATUS_PUBLISH_TOKEN=test-only\n')
            output=root/'status.json';output.write_text(json.dumps({'products':[{'status':'unknown'}]}))
            stamp=root/'stamp'
            with patch.multiple(publish_status, CONFIG=config, OUTPUT=output, STAMP=stamp), patch.object(publish_status,'urlopen') as send:
                send.side_effect=OSError('network')
                with self.assertRaises(OSError):publish_status.main()
                self.assertFalse(stamp.exists())
                send.side_effect=None
                send.return_value.__enter__.return_value.status=204
                publish_status.main()
                self.assertTrue(stamp.exists())
                request=send.call_args.args[0]
                self.assertNotIn(b'test-only',request.data)
                publish_status.main()
                self.assertEqual(send.call_count,2)
