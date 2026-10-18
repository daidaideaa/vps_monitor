"""Disconnect recovery and short stock windows; no Telegram or SMTP requests."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from observatory.store import Store
from observatory.stock import accept, parse_message, snapshot
from observatory.telegram_service import check_connection


class TelegramRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.root.name) / 'stock.sqlite')
        self.client = Mock()
        self.client.is_connected.return_value = False
        self.client.disconnected.done.return_value = False
        self.client.disconnect = AsyncMock()
        self.client.catch_up = AsyncMock()

    def tearDown(self):
        self.store.db.close()
        self.root.cleanup()

    async def test_exhausted_reconnect_exits_instead_of_idle_service(self):
        self.client.disconnected.done.return_value = True
        with self.assertRaisesRegex(ConnectionError, 'restart required'):
            await check_connection(self.client, self.store, None, now=100)
        self.assertEqual(self.store.get('telegram_health')['state'], 'disconnected')
        self.client.disconnect.assert_awaited_once()

    async def test_stalled_reconnect_has_bounded_grace_period(self):
        since = await check_connection(self.client, self.store, None, now=100)
        self.assertEqual(self.store.get('telegram_health')['state'], 'disconnected')
        since = await check_connection(self.client, self.store, since, now=129)
        self.client.disconnect.assert_not_awaited()
        with self.assertRaises(ConnectionError):
            await check_connection(self.client, self.store, since, now=130)
        self.client.disconnect.assert_awaited_once()

    async def test_recovery_catches_up_once_without_idle_history_polling(self):
        since = await check_connection(self.client, self.store, None, now=100)
        self.client.is_connected.return_value = True
        since = await check_connection(self.client, self.store, since, now=105)
        self.assertIsNone(since)
        await check_connection(self.client, self.store, since, now=500)
        self.client.catch_up.assert_awaited_once()
        self.client.disconnect.assert_not_awaited()

    async def test_basic_three_minute_window_queues_before_sold_out_edit(self):
        now = time.time()
        text = 'VMISS - JP.TKY.STD.Basic\n✅ 补货\nhttps://app.vmiss.com/cart.php?pid=101'
        fresh = parse_message('vmisstz', 900, text, now)
        accept(self.store, fresh, now=now, channels=('email',))
        # The notification exists at receipt; it does not wait for the next page refresh.
        self.assertEqual(self.store.db.execute('SELECT channel FROM outbox').fetchall(), [('email',)])
        sold = parse_message('vmisstz', 900, text.replace('✅ 补货', '❌ 售罄'), now+180)
        accept(self.store, sold, now=now+180, channels=('email',))
        self.assertEqual(snapshot(self.store, now+180)['products'][0]['status'], 'unavailable')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.assertIsNone(parse_message('vmisstz', 901, text.replace('STD.Basic', 'BGP.Basic'), now))


if __name__ == '__main__':
    unittest.main()
