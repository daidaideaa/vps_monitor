"""A-only DNS query pinned to the physical interface; rejects TUN fake addresses."""
import ipaddress
import os
import secrets
import socket
import struct

def ipv4(host,source,index,resolver='223.5.5.5'):
    try:
        ip=ipaddress.ip_address(host)
        return str(ip)
    except ValueError:pass
    identifier=secrets.randbelow(65536)
    name=b''.join(bytes([len(label)])+label.encode('idna') for label in host.rstrip('.').split('.'))+b'\0'
    query=struct.pack('!6H',identifier,0x100,1,0,0,0)+name+struct.pack('!HH',1,1)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        if source:sock.bind((source,0))
        if os.name=='nt' and index:
            sock.setsockopt(socket.IPPROTO_IP,31,socket.htonl(index)) # IP_UNICAST_IF
        sock.sendto(query,(resolver,53));data,peer=sock.recvfrom(4096)
    if peer[0]!=resolver or len(data)<12:raise ValueError('invalid_dns_peer')
    ident,flags,questions,answers,_,_=struct.unpack('!6H',data[:12])
    if ident!=identifier or flags&0xf or not flags&0x8000:raise ValueError('invalid_dns_answer')
    def skip(offset):
        while True:
            size=data[offset];offset+=1
            if size==0:return offset
            if size&0xc0==0xc0:return offset+1
            offset+=size
    pos=12
    for _ in range(questions):pos=skip(pos)+4
    for _ in range(answers):
        pos=skip(pos)
        typ,klass,ttl,length=struct.unpack('!HHIH',data[pos:pos+10]);pos+=10
        if typ==1 and klass==1 and length==4:
            address=ipaddress.ip_address(data[pos:pos+4])
            if not address.is_global or address in ipaddress.ip_network('198.18.0.0/15'):
                raise ValueError('non_public_dns_answer')
            return str(address)
        pos+=length
    raise ValueError('no_ipv4_answer')
