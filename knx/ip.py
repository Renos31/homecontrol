import socket
import threading
import SocketServer
import sys
import logging

from knx.core import KNXException, ValueCache
from knx.helper import *

is_py2 = sys.version[0] == '2'
if is_py2:
    import Queue as queue
else:
    import queue as queue

class KNXIPFrame():
    
    SEARCH_REQUEST                  = 0x0201
    SEARCH_RESPONSE                 = 0x0202
    DESCRIPTION_REQUEST             = 0x0203
    DESCRIPTION_RESPONSE            = 0x0204
    CONNECT_REQUEST                 = 0x0205
    CONNECT_RESPONSE                = 0x0206
    CONNECTIONSTATE_REQUEST         = 0x0207
    CONNECTIONSTATE_RESPONSE        = 0x0208
    DISCONNECT_REQUEST              = 0x0209
    DISCONNECT_RESPONSE             = 0x020a
    DEVICE_CONFIGURATION_REQUEST    = 0x0310
    DEVICE_CONFIGURATION_ACK        = 0x0111
    TUNNELING_REQUEST               = 0x0420
    TUNNELLING_ACK                  = 0x0421
    ROUTING_INDICATION              = 0x0530
    ROUTING_LOST_MESSAGE            = 0x0531
    
    DEVICE_MGMT_CONNECTION          = 0x03
    TUNNEL_CONNECTION               = 0x04
    REMLOG_CONNECTION               = 0x06
    REMCONF_CONNECTION              = 0x07
    OBJSVR_CONNECTION               = 0x08
    
    E_NO_ERROR                      = 0x00
    E_HOST_PROTOCOL_TYPE            = 0x01
    E_VERSION_NOT_SUPPORTED         = 0x02
    E_SEQUENCE_NUMBER               = 0x04
    E_CONNECTION_ID                 = 0x21
    E_CONNECTION_TYPE               = 0x22
    E_CONNECTION_OPTION             = 0x23
    E_NO_MORE_CONNECTIONS           = 0x24
    E_DATA_CONNECTION               = 0x26
    E_KNX_CONNECTION                = 0x27
    E_TUNNELING_LAYER               = 0x28
    
    body = None
    
    def __init__(self, service_type_id):
        self.service_type_id = service_type_id
    
    def to_frame(self):
        return self.header()+self.body
    
    @classmethod
    def from_frame(cls, frame):
        # TODO: Check length
        p = cls(frame[2]*256 + frame[3])
        p.body = frame[6:]
        return p
        
    def total_length(self):
        return 6 + len(self.body)
    
    def header(self):
        tl = self.total_length()
        res = [0x06,0x10,0,0,0,0]
        res[2] = (self.service_type_id >> 8) & 0xff
        res[3] = (self.service_type_id >> 0) & 0xff
        res[4] = (tl >> 8) & 0xff
        res[5] = (tl >> 0) & 0xff
        return res
    
class KNXTunnelingRequest:
    
    seq = 0
    cEmi = None
    channel = 0
    
    def __init__(self):
        pass
        
    @classmethod
    def from_body(cls, body):
        # TODO: Check length
        p = cls()
        p.channel = body[1]
        p.seq = body[2]
        p.cEmi = body[4:]
        return p
    
    def __str__(self):
        return ""

class CEMIMessage():
    
    CMD_GROUP_READ = 1
    CMD_GROUP_WRITE = 2
    CMD_GROUP_RESPONSE = 3
    CMD_UNKNOWN = 0xff
    
    code = 0
    ctl1 = 0
    ctl2 = 0
    src_addr = None
    dst_addr = None
    cmd = None
    tpci_apci = 0
    mpdu_len = 0
    data = [0]
    
    def __init__(self):
        pass    
    
    @classmethod
    def from_body(cls, cemi):
        # TODO: check that length matches
        m = cls()
        m.code = cemi[0]
        offset = cemi[1]
        
        m.ctl1 = cemi[2+offset]
        m.ctl2 = cemi[3+offset]
        
        m.src_addr = cemi[4+offset]*256+cemi[5+offset]
        m.dst_addr = cemi[6+offset]*256+cemi[7+offset]
    
        m.mpdu_len = cemi[8+offset]
        
        tpci_apci = cemi[9+offset]*256+cemi[10+offset]
        apci = tpci_apci & 0x3ff
        
        # for APCI codes see KNX Standard 03/03/07 Application layer 
        # table Application Layer control field
        if (apci & 0x080):
            # Group write
            m.cmd = CEMIMessage.CMD_GROUP_WRITE
        elif (apci == 0):
            m.cmd = CEMIMessage.CMD_GROUP_READ
        elif (apci & 0x40):
            m.cmd = CEMIMessage.CMD_GROUP_RESPONSE
        else:
            m.cmd = CEMIMessage.CMD_NOT_IMPLEMENTED
        
        apdu = cemi[10+offset:]
        if len(apdu) != m.mpdu_len:
            raise KNXException("APDU LEN should be {} but is {}".format(m.mpdu_len,len(apdu)))
        
        if len(apdu)==1:
            m.data = [apci & 0x2f]
        else:
            m.data = cemi[11+offset:]
        
        return m
    
    def init_group(self,dst_addr=1):
        self.code = 0x11 # Comes from packet dump, why?
        self.ctl1 = 0xbc # frametype 1, repeat 1, system broadcast 1, priority 3, ack-req 0, confirm-flag 0
        self.ctl2 = 0xe0 # dst addr type 1, hop count 6, extended frame format
        self.src_addr = 0
        self.dst_addr = dst_addr
    
    def init_group_write(self, dst_addr=1, data=[0]):
        self.init_group(dst_addr)
        self.tpci_apci = 0x00 * 256 + 0x80 # unnumbered data packet, group write
        self.data = data
    
    def init_group_read(self, dst_addr=1):
        self.init_group(dst_addr)
        self.tpci_apci = 0x00 # unnumbered data packet, group read
        self.data = [0]

    def to_body(self):
        b = [self.code,0x00,self.ctl1,self.ctl2,
             (self.src_addr >> 8) & 0xff, (self.src_addr >> 0) & 0xff,
             (self.dst_addr >> 8) & 0xff, (self.dst_addr >> 0) & 0xff,
             ]
        if (len(self.data)==1) and ((self.data[0] & 3) == self.data[0]) :
            # less than 6 bit of data, pack into APCI byte
            b.extend([1,(self.tpci_apci >> 8) & 0xff,((self.tpci_apci >> 0) & 0xff) + self.data[0]])
        else:
            b.extend([1+len(self.data),(self.tpci_apci >> 8) & 0xff,(self.tpci_apci >> 0) & 0xff])
            b.extend(self.data)
        
        return b
        
    def __str__(self):
        c="??"
        if self.cmd == self.CMD_GROUP_READ:
            c = "RD"
        elif self.cmd == self.CMD_GROUP_WRITE:
            c = "WR"
        elif self.cmd == self.CMD_GROUP_RESPONSE:
            c = "RS"
        return "{0:x}->{1:x} {2} {3}".format(self.src_addr, self.dst_addr, c, self.data)
    
    
class KNXIPTunnel():
    
    # TODO: implement a control server
    #    control_server = None
    data_server = None
    control_socket = None
    channel = 0
    seq = 0
    valueCache = None
    data_handler = None
    result_queue = None
    
    def __init__(self, ip, port, valueCache=None):
        self.remote_ip = ip
        self.remote_port = port
        self.discovery_port = None
        self.data_port = None
        self.result_queue = queue.Queue()
        self.unack_queue = queue.Queue()
        if valueCache == None:
            self.valueCache = ValueCache()
        else:
            self.valueCache = ValueCache

    def connect(self):
        # Find my own IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((self.remote_ip,self.remote_port))
        local_ip=s.getsockname()[0]

        if self.data_server:
            logging.info("Data server already running, not starting again")
        else:
            self.data_server = DataServer((local_ip, 0), DataRequestHandler)
            self.data_server.tunnel = self 
            _ip, self.data_port = self.data_server.server_address
            data_server_thread = threading.Thread(target=self.data_server.serve_forever)
            data_server_thread.daemon = True
            data_server_thread.start()
            
        self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.control_socket.bind((local_ip, 0))
        
        # Connect packet
        p=[]
        p.extend([0x06,0x10]) # header size, protocol version
        p.extend(int_to_array(KNXIPFrame.CONNECT_REQUEST , 2))
        p.extend([0x00,0x1a]) # total length = 24 octet
        
        # Control endpoint
        p.extend([0x08,0x01]) # length 8 bytes, UPD
        _ip,port=self.control_socket.getsockname()
        p.extend(ip_to_array(local_ip))
        p.extend(int_to_array(port, 2)) 
        
        # Data endpoint
        p.extend([0x08,0x01]) # length 8 bytes, UPD
        p.extend(ip_to_array(local_ip))
        p.extend(int_to_array(self.data_port, 2)) 

        # 
        p.extend([0x04,0x04,0x02,0x00])
        
        self.control_socket.sendto("".join(map(chr, p)), (self.remote_ip, self.remote_port))
        
        #TODO: non-blocking receive
        received = self.control_socket.recv(1024)
        
        # Check if the response is an TUNNELING ACK
        r_sid = ord(received[2])*256+ord(received[3])
        if r_sid == KNXIPFrame.CONNECT_RESPONSE:
            self.channel = ord(received[6])
            logging.debug("Connected KNX IP tunnel (Channel: {})".format(self.channel,self.seq))
            # TODO: parse the other parts of the response
        else:
            raise KNXException("Could not initiate tunnel connection, STI = {0:x}".format(r_sid))
        
    def send_tunnelling_request(self, cemi):
        f = KNXIPFrame(KNXIPFrame.TUNNELING_REQUEST)
        b = [0x04,self.channel,self.seq,0x00] # Connection header see KNXnet/IP 4.4.6 TUNNELLING_REQUEST
        if (self.seq < 0xff):
            self.seq += 1
        else:
            self.seq = 0
        b.extend(cemi.to_body())
        f.body=b
        self.data_server.socket.sendto(bytes_to_str(f.to_frame()), (self.remote_ip, self.remote_port))
        # TODO: wait for ack
        
        
    def group_read(self, addr, use_cache=True):
        if use_cache:
            res = self.valueCache.get(addr)
            if res:
                logging.debug("Got value of group address {} from cache: {}".format(addr,res))
                return res
        
        cemi = CEMIMessage()
        cemi.init_group_read(addr)
        self.send_tunnelling_request(cemi)
        # Wait for the result
        res = self.result_queue.get()
        self.result_queue.task_done()
        return res
    
    def group_write(self, addr, data):
        cemi = CEMIMessage()
        cemi.init_group_write(addr, data)
        self.send_tunnelling_request(cemi)
    
    def group_toggle(self,addr, use_cache=True):
        d = self.group_read(addr, use_cache)
        if len(d) != 1:
            problem="Can't toggle a {}-octet group address {}".format(len(d),addr)
            logging.error(problem)
            raise KNXException(problem)
        
        if (d[0]==0):
            self.group_write(addr, [1])
        elif (d[0]==1):
            self.group_write(addr, [0])
        else:
            problem="Can't toggle group address {} as value is {}".format(addr,d[0])
            logging.error(problem)
            raise KNXException(problem)
            
    
class DataRequestHandler(SocketServer.BaseRequestHandler):
    
    def handle(self):
        data = str_to_bytes(self.request[0])
        socket = self.request[1]
        
        f = KNXIPFrame.from_frame(data)
        
        if f.service_type_id == KNXIPFrame.TUNNELING_REQUEST:
            req = KNXTunnelingRequest.from_body(f.body)            
            msg = CEMIMessage.from_body(req.cEmi)
            send_ack = False
            
            # print(msg)
            tunnel = self.server.tunnel
            
            if msg.code == 0x29:
                # LData.req
                send_ack = True
            elif msg.code == 0x2e:
                # LData.con
                send_ack = True
            else: 
                problem="Unimplemented cEMI message code {}".format(msg.code)
                logging.error(problem)
                raise KNXException(problem)
            
            logging.debug("Received KNX message {}".format(msg))
            
            # Cache data
            if (msg.cmd == CEMIMessage.CMD_GROUP_WRITE) or (msg.cmd == CEMIMessage.CMD_GROUP_RESPONSE):
                    # saw a value for a group address on the bus
                tunnel.valueCache.set(msg.dst_addr,msg.data)
                    
            # Put RESPONSES into the result queue
            if (msg.cmd == CEMIMessage.CMD_GROUP_RESPONSE):
                tunnel.result_queue.put(msg.data)
            
            if send_ack:
                bodyack = [0x04, req.channel, req.seq, KNXIPFrame.E_NO_ERROR]
                ack = KNXIPFrame(KNXIPFrame.TUNNELLING_ACK)
                ack.body = bodyack
                socket.sendto(bytes_to_str(ack.to_frame()), self.client_address)
        
 
class DataServer(SocketServer.ThreadingMixIn, SocketServer.UDPServer):
    pass
