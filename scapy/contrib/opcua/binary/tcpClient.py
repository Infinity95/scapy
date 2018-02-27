# coding=utf-8
import socket

import os

from scapy.contrib.opcua.helpers import UaConnectionContext
from scapy.contrib.opcua.binary.networking import chunkify
from scapy.automaton import ATMT
from scapy.contrib.opcua.binary.automaton import _UaAutomaton
from scapy.contrib.opcua.binary.uaTypes import *
from scapy.supersocket import SuperSocket
import scapy.contrib.opcua.binary.uaTypes as UA


class _TcpSuperSocket(SuperSocket):
    
    def __init__(self, connectionContext, family=socket.AF_INET, type=socket.SOCK_STREAM):
        self.socket = socket.socket(family, type)
        self.outs = self.socket
        self.ins = self.socket
        self.logger = logging.getLogger(__name__)
        self.connectionContext = connectionContext
        self.open = False
    
    def _send(self, chunk):
        chunk = bytes(chunk)
        sent = 0
        try:
            while sent < len(chunk):
                part = self.socket.send(chunk[sent:])
                if part == 0:
                    raise RuntimeError("Connection broke")
                sent += part
        except BrokenPipeError as e:
            print(e)
            raise
        self.connectionContext.sendSequenceNumber += 1
    
    def send(self, data):
        if not self.open:
            self.logger.warning("Connection not open. No data sent.")
            return
        if not isinstance(data, UaTcp):
            self.logger.warning("Unsupported packet type. No data sent.")
            return
        data.connectionContext = self.connectionContext
        
        if isinstance(data, UaSecureConversationSymmetric):
            chunks = chunkify(data)
        else:
            chunks = [data]
        
        for chunk in chunks:
            self._send(chunk)
    
    def recv(self, x=0):
        if not self.open:
            raise ConnectionError("Connection not open")
        headerLen = len(UA.UaTcpMessageHeader())
        header = self.socket.recv(headerLen)
        if not header:
            self.logger.warning("TCP socket got disconnected")
            self.open = False
            return None
        
        decodedHeader = UA.UaTcpMessageHeader(header)
        size = decodedHeader.MessageSize - headerLen
        
        body = self.socket.recv(size)
        if not body:
            self.logger.warning("Could not receive body. expected {} bytes".format(size))
            return None
        pkt = UA.UaTcp(header + body, connectionContext=self.connectionContext)
        # print("Received packet: ")
        # pkt.show()
        return pkt
    
    def connect(self, target):
        if not self.open:
            self.socket.connect(target)
            self.open = True
    
    def close(self):
        if self.open:
            self.socket.shutdown(socket.SHUT_WR)
            self.socket.close()
            self.open = False
    
    def sr(self, *args, **kargs):
        raise NotImplementedError()
    
    def sr1(self, *args, **kargs):
        raise NotImplementedError()
    
    def sniff(self, *args, **kargs):
        raise NotImplementedError()
    
    def fileno(self):
        return self.socket.fileno()


class TcpClientAutomaton(_UaAutomaton):
    """
    This Automaton implements the ua tcp layer functionality.
    It can be used as part of an automaton that implements the SecureChannel layer.
    """
    
    def parse_args(self, *args, **kwargs):
        super(TcpClientAutomaton, self).parse_args(*args, **kwargs)
    
    @ATMT.state(initial=1)
    def START(self):
        pass
    
    @ATMT.state()
    def TCP_CONNECTED(self):
        pass
    
    @ATMT.state()
    def CONNECTING(self):
        pass
    
    @ATMT.state()
    def CONNECTED(self):
        pass
    
    @ATMT.state()
    def TCP_DISCONNECTING(self):
        pass
    
    @ATMT.state()
    def TCP_DISCONNECTED(self):
        pass
    
    @ATMT.state(final=1)
    def END(self):
        # Send None to signal that the socket is closed
        self.oi.uatcp.send(None)
    
    @ATMT.condition(START)
    def connectTCP(self):
        self.send_sock = _TcpSuperSocket(self.connectionContext)
        self.listen_sock = self.send_sock
        
        try:
            self.send_sock.connect((self.target, self.targetPort))
            self.logger.debug("TCP connected")
        except socket.error as e:
            self.logger.warning("TCP connection refused: {}".format(e))
            raise self.END()
        raise self.TCP_CONNECTED()
    
    @ATMT.condition(TCP_CONNECTED)
    def connect(self):
        self.logger.debug("Sending HEL")
        self.send(UaTcp(Message=UaTcpHelloMessage(), connectionContext=self.connectionContext))
        raise self.CONNECTING()
    
    @ATMT.receive_condition(CONNECTING)
    def receive_ack(self, pkt):
        if isinstance(pkt, UaTcp) and isinstance(pkt.Message, UaTcpAcknowledgeMessage):
            self.logger.debug("Received ACK")
            self.connectionContext.remoteBufferSizes.maxChunkCount = pkt.Message.MaxChunkCount
            self.connectionContext.remoteBufferSizes.maxMessageSize = pkt.Message.MaxMessageSize
            self.connectionContext.protocolVersion = pkt.Message.ProtocolVersion
            self.connectionContext.remoteBufferSizes.receiveBufferSize = pkt.Message.ReceiveBufferSize
            self.connectionContext.remoteBufferSizes.sendBufferSize = pkt.Message.SendBufferSize
            raise self.CONNECTED()
        elif isinstance(pkt, UaTcp) and isinstance(pkt.Message, UaTcpErrorMessage):
            self.logger.debug("Received ERR: {}".format(statusCodes[pkt.Message.Error]))
            raise self.TCP_DISCONNECTING()
        else:
            self.logger.debug("Unexpected message received")
            raise self.TCP_DISCONNECTING()
    
    @ATMT.condition(TCP_DISCONNECTING)
    def tcp_disconnect(self):
        self.send_sock.close()
        self.logger.debug("TCP socket disconnected")
        raise self.TCP_DISCONNECTED()
    
    @ATMT.condition(TCP_DISCONNECTED)
    def end(self):
        raise self.END()
    
    @ATMT.receive_condition(CONNECTED, prio=1)
    def receive_response(self, pkt):
        print("Receiving")
        self.oi.uatcp.send(pkt)
        raise self.CONNECTED()
    
    @ATMT.receive_condition(CONNECTED, prio=0)
    def error_received(self, pkt):
        if type(pkt) is UaTcp and isinstance(pkt.Message, UaTcpErrorMessage):
            self.logger.warning("ERR received: {} ... Closing connection".format(statusCodes[pkt.Message.Error]))
            raise self.TCP_DISCONNECTING()
        elif not isinstance(pkt, UaTcp):
            self.logger.warning("Unexpected message received... Closing connection")
            raise self.TCP_DISCONNECTING()
    
    @ATMT.ioevent(CONNECTED, "uatcp")
    def socket_send(self, fd):
        raise self.CONNECTED().action_parameters(fd.recv())
    
    @ATMT.ioevent(CONNECTED, "shutdown")
    def shutdown(self, fd):
        raise self.TCP_DISCONNECTING()
    
    @ATMT.action(socket_send)
    def send_data(self, data):
        self.send(data)


class UaTcpSocket(SuperSocket):
    
    def __init__(self, connectionContext, target="localhost", targetPort=4840, endpoint="TODO"):
        self.atmt = TcpClientAutomaton(connectionContext=connectionContext, target=target, targetPort=targetPort)
        self.atmt.runbg()
        self.open = True
        self.logger = logging.getLogger(__name__)

    def send(self, data):
        if not self.open:
            self.logger.warning("Socket not open. No data sent.")
            return
        self.atmt.io.uatcp.send(data)

    def recv(self, x=0):
        if not self.open:
            self.logger.warning("Socket not open. Cannot receive any data.")
            return None
        data = self.atmt.io.uatcp.recv()
        if data is None:
            self.close()
        return data

    def fileno(self):
        return self.atmt.io.uatcp.fileno()

    def connect(self):
        if not self.open:
            self.atmt.start()
            self.atmt.runbg()
            self.open = True

    def close(self):
        # TODO: Stop gracefully
        if self.open:
            self.atmt.io.shutdown.send(None)
            self.atmt.stop()
            self.open = False

    def sr(self, *args, **kargs):
        raise NotImplementedError()

    def sr1(self, *args, **kargs):
        raise NotImplementedError()

    def sniff(self, *args, **kargs):
        raise NotImplementedError()
