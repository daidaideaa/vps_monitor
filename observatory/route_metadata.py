"""Bounded UDP traceroute fallback for minimal Linux hosts; no packet content persisted."""
import socket
import struct
import time

def trace(host,hops=12,timeout=.5):
    address=socket.gethostbyname(host);records=[];port=33490
    with socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP) as incoming, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as outgoing:
        incoming.settimeout(timeout)
        for ttl in range(1,hops+1):
            outgoing.setsockopt(socket.SOL_IP,socket.IP_TTL,ttl)
            started=time.monotonic();outgoing.sendto(b'',(address,port+ttl));reply=None
            while time.monotonic()-started<timeout:
                try:data,peer=incoming.recvfrom(256)
                except socket.timeout:break
                ihl=(data[0]&15)*4
                if len(data)<ihl+8+28:continue
                inner=data[ihl+8:];inner_len=(inner[0]&15)*4
                if inner[9]!=17 or len(inner)<inner_len+8:continue
                dest=struct.unpack('!H',inner[inner_len+2:inner_len+4])[0]
                if dest!=port+ttl or socket.inet_ntoa(inner[16:20])!=address:continue
                reply={'hop':ttl,'address':peer[0],'ms':round((time.monotonic()-started)*1000,2),'icmp_type':data[ihl]};break
            records.append(reply or {'hop':ttl,'state':'no_reply'})
            if reply and reply['address']==address:break
    return {'method':'bounded_udp_ttl','target':address,'hops':records,'note':'中间跳不应答不能证明该跳丢包。'}
