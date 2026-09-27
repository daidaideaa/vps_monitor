"""Real isolated loopback traffic verifies both kernel packet directions."""
import concurrent.futures
import json
import socket
import sys
import time
import unittest

from observatory.udp_metadata import capture


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux packet sockets required')
class UdpMetadataTests(unittest.TestCase):
    def test_directions_and_filter_without_payload(self):
        for by_host in (False,True):
            with self.subTest(by_host=by_host):
                self.check_capture(by_host)

    def check_capture(self,by_host):
        try:
            probe=socket.socket(socket.AF_PACKET,socket.SOCK_DGRAM,socket.htons(3))
        except PermissionError:
            self.skipTest('CAP_NET_RAW required')
        probe.close()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as target, \
             socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as other, \
             socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender, \
             concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            target.bind(('127.0.0.2',0));other.bind(('127.0.0.1',0))
            port=target.getsockname()[1]
            future=pool.submit(capture,'127.0.0.2' if by_host else None,1.2,100,None if by_host else port)
            # Repeated loopback datagrams avoid racing the capture socket setup.
            for _ in range(8):
                sender.sendto(b'PAYLOAD_MUST_NOT_PERSIST',target.getsockname())
                sender.sendto(b'EXCLUDED_PORT',other.getsockname())
                time.sleep(.08)
            data=future.result(timeout=4)
        self.assertEqual({r['direction'] for r in data['headers']},{'in','out'})
        self.assertTrue(all(port in (r['sport'],r['dport']) for r in data['headers']))
        self.assertEqual(data['capture_version'],2)
        self.assertEqual(data['direction_source'],'sockaddr_ll.packet_type')
        self.assertFalse(data['payload_persisted'])
        self.assertNotIn('PAYLOAD_MUST_NOT_PERSIST',json.dumps(data))
        self.assertNotIn('EXCLUDED_PORT',json.dumps(data))


if __name__=='__main__':
    unittest.main()
