"""Minimalni klient Source RCON pro ARK.

Zamerne bez externi zavislosti - viz requirements.txt.
"""
import socket
import struct
import threading

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

# ARK vraci tohle misto prazdne odpovedi, napr. kdyz v GetChat nic neni.
ARK_EMPTY = "Server received, But no response!!"


class RconError(Exception):
    pass


class RconClient:
    """Synchronni RCON klient. Jedna instance na mapu, chraneny zamkem."""

    def __init__(self, host, port, password, timeout=10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock = None
        self._next_id = 1
        self._lock = threading.Lock()

    # --- spojeni ---

    def connect(self):
        with self._lock:
            self._connect_locked()

    def _connect_locked(self):
        self._close_locked()
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        req_id = self._send_locked(SERVERDATA_AUTH, self.password)
        # Po auth chodi nekdy prazdny RESPONSE_VALUE pred AUTH_RESPONSE.
        while True:
            pkt_id, pkt_type, _ = self._recv_locked()
            if pkt_type == SERVERDATA_AUTH_RESPONSE:
                if pkt_id == -1:
                    self._close_locked()
                    raise RconError("RCON: spatne heslo")
                if pkt_id != req_id:
                    self._close_locked()
                    raise RconError("RCON: neocekavane id v odpovedi auth")
                return

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self):
        return self._sock is not None

    # --- prikazy ---

    def command(self, cmd, retry=True):
        """Posle prikaz a vrati odpoved. Pri padlem spojeni se jednou znovu pripoji."""
        with self._lock:
            try:
                if self._sock is None:
                    self._connect_locked()
                return self._command_locked(cmd)
            except (OSError, RconError):
                self._close_locked()
                if not retry:
                    raise
                self._connect_locked()
                return self._command_locked(cmd)

    def _command_locked(self, cmd):
        req_id = self._send_locked(SERVERDATA_EXECCOMMAND, cmd)
        parts = []
        while True:
            pkt_id, _, body = self._recv_locked()
            if pkt_id != req_id:
                continue
            parts.append(body)
            # ARK posila odpoved v jednom paketu; delsi vypisy se deli po 4096 B.
            if len(body) < 4000:
                break
        out = "".join(parts).strip()
        return "" if out == ARK_EMPTY else out

    # --- protokol ---

    def _send_locked(self, pkt_type, body):
        req_id = self._next_id
        self._next_id = self._next_id % 0x7FFFFFFF + 1
        payload = struct.pack("<ii", req_id, pkt_type) + body.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        return req_id

    def _recv_locked(self):
        raw_len = self._recv_exactly(4)
        (length,) = struct.unpack("<i", raw_len)
        if not 10 <= length <= 4 * 1024 * 1024:
            raise RconError(f"RCON: nesmyslna delka paketu {length}")
        payload = self._recv_exactly(length)
        pkt_id, pkt_type = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", errors="replace")
        return pkt_id, pkt_type, body

    def _recv_exactly(self, count):
        buf = b""
        while len(buf) < count:
            chunk = self._sock.recv(count - len(buf))
            if not chunk:
                raise RconError("RCON: spojeni zavreno protejskem")
            buf += chunk
        return buf
