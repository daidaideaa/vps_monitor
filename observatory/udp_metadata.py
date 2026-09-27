"""Linux packet metadata only: kernel BPF limits capture to a target's IPv4 UDP."""
import ctypes
import socket
import struct
import time

def capture(host,seconds=12,limit=120,port=None):
    address=socket.gethostbyname(host) if host else None
    target=int.from_bytes(socket.inet_aton(address),'big') if address else 0
    class Filter(ctypes.Structure):
        _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_=[('length',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
    # AF_PACKET/SOCK_DGRAM begins at the network header, regardless of NIC link-layer type.
    instructions=(Filter*8)(Filter(0x30,0,0,9),Filter(0x15,0,5,17),
        Filter(0x20,0,0,12),Filter(0x15,2,0,target),Filter(0x20,0,0,16),
        Filter(0x15,0,1,target),Filter(0x06,0,0,96),Filter(0x06,0,0,0))
    if port:
        instructions=(Filter*9)(Filter(0x30,0,0,9),Filter(0x15,0,5,17),Filter(0xb1,0,0,0),
            Filter(0x48,0,0,0),Filter(0x15,3,0,port),Filter(0x48,0,0,2),Filter(0x15,1,0,port),
            Filter(0x06,0,0,0),Filter(0x06,0,0,96))
    program=Program(len(instructions),instructions);records=[];start=time.time()
    with socket.socket(socket.AF_PACKET,socket.SOCK_DGRAM,socket.htons(0x0800)) as sock:
        library=ctypes.CDLL(None,use_errno=True)
        if library.setsockopt(sock.fileno(),socket.SOL_SOCKET,26,ctypes.byref(program),ctypes.sizeof(program))!=0:
            raise OSError(ctypes.get_errno(),'attach_target_filter_failed')
        sock.settimeout(.5)
        while time.time()-start<seconds and len(records)<limit:
            try:data,nic=sock.recvfrom(96)
            except socket.timeout:continue
            if len(data)<28 or data[0]>>4!=4 or data[9]!=17:continue
            if struct.unpack('!H',data[6:8])[0]&0x1fff:continue
            ihl=(data[0]&15)*4
            if len(data)<ihl+8:continue
            src=socket.inet_ntoa(data[12:16]);dst=socket.inet_ntoa(data[16:20])
            if address and address not in (src,dst):continue
            sport,dport,length,_=struct.unpack('!HHHH',data[ihl:ihl+8])
            if port and port not in (sport,dport):continue
            records.append({'at':time.time(),'interface':nic[0],'direction':'in' if (dport==port if port else src==address) else 'out',
                'src':src,'dst':dst,'sport':sport,'dport':dport,'udp_bytes':length})
        packets,drops=struct.unpack('II',sock.getsockopt(263,6,8))
    return {'tool':'AF_PACKET/BPF','state':'ok','target':address,'started_at':start,'ended_at':time.time(),
        'packets_seen':packets,'capture_drops':drops,'headers':records,'payload_persisted':False}
