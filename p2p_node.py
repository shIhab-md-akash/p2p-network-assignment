"""
p2p_node.py - The P2P node: every peer is a TCP server AND a TCP client.

* Server role: a listening socket accepts connections from other peers.
* Client role: connect() opens a connection to another peer.
* Every connection (incoming or outgoing) is handled by its own thread, so a
  peer can talk to many peers at the same time.
* There is no central server: messages and files go straight from peer to peer.

The node knows nothing about the user interface. It reports what happens
through two callbacks:

    log_callback(text)     - a line for the message/event log
    peers_callback()       - the list of connected peers changed
"""

import ipaddress
import os
import socket
import threading
import uuid

from protocol import (
    CHUNK_SIZE,
    MSG_ERROR,
    MSG_FILE,
    MSG_HELLO,
    MSG_HELLO_ACK,
    MSG_TEXT,
    ConnectionClosed,
    ProtocolError,
    make_error,
    make_file,
    make_hello,
    make_hello_ack,
    make_text,
    recv_message,
    send_message,
)

CONNECT_TIMEOUT = 5      # seconds to wait when connecting to another peer
HANDSHAKE_TIMEOUT = 5    # seconds to wait for the HELLO exchange


class Peer:
    """One connected remote peer."""

    def __init__(self, peer_id, name, ip, port, sock):
        self.peer_id = peer_id
        self.name = name
        self.ip = ip
        self.port = port          # the port the remote peer is LISTENING on
        self.sock = sock
        # Only one thread may write to a socket at a time, otherwise a text
        # message could end up in the middle of a file's bytes.
        self.send_lock = threading.Lock()

    @property
    def label(self):
        return f"{self.name} [{self.peer_id}]"

    def __str__(self):
        return f"{self.label} {self.ip}:{self.port}"


class P2PNode:
    def __init__(self, name, port, log_callback=None, peers_callback=None,
                 download_dir="downloads"):
        self.name = str(name).strip()
        self.port = port
        self.peer_id = uuid.uuid4().hex[:8]
        self.download_dir = download_dir
        self._log_callback = log_callback or (lambda text: None)
        self._peers_callback = peers_callback or (lambda: None)

        self._peers = {}                 # peer_id -> Peer
        self._peers_lock = threading.Lock()
        self._server = None
        self._accept_thread = None
        self._running = False

    # ------------------------------------------------------------------ utils
    def log(self, text):
        self._log_callback(text)

    @property
    def running(self):
        return self._running

    def list_peers(self):
        with self._peers_lock:
            return sorted(self._peers.values(), key=lambda p: p.name.lower())

    def get_peer(self, peer_id):
        with self._peers_lock:
            return self._peers.get(peer_id)

    # ------------------------------------------------------------ start / stop
    def start(self):
        """Create the listening socket and start accepting connections."""
        if not self.name:
            raise ValueError("Peer name cannot be empty")
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ValueError("Port must be a number between 1 and 65535")
        if self._running:
            raise RuntimeError("Peer is already running")

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # On Windows SO_REUSEADDR would let two programs share one port, so it
        # is only enabled on Linux/macOS (where it just avoids TIME_WAIT delays).
        if os.name != "nt":
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", self.port))
            server.listen()
        except OSError as exc:
            server.close()
            raise OSError(f"Cannot listen on port {self.port}: {exc.strerror or exc}")
        server.settimeout(0.5)           # lets the accept loop notice stop()
        self._server = server
        self._running = True
        os.makedirs(self.download_dir, exist_ok=True)

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self.log(f"[SYSTEM] Peer started: {self.name} [{self.peer_id}] on port {self.port}")

    def stop(self):
        """Stop listening and close every connection."""
        if not self._running:
            return
        self._running = False
        try:
            self._server.close()
        except OSError:
            pass
        # Wait for the accept thread to finish so the port is really released
        # (otherwise pressing Stop and then Start again could fail).
        if self._accept_thread and self._accept_thread is not threading.current_thread():
            self._accept_thread.join(timeout=2)
        for peer in self.list_peers():
            self._remove_peer(peer, "peer stopped", announce=False)
        self.log("[SYSTEM] Peer stopped")

    # --------------------------------------------------------- server role
    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break                    # server socket was closed
            # One thread per incoming connection.
            threading.Thread(target=self._handle_incoming, args=(conn, addr),
                             daemon=True).start()

    def _handle_incoming(self, conn, addr):
        """Handshake with a peer that connected to us, then listen to it."""
        peer = None
        try:
            conn.settimeout(HANDSHAKE_TIMEOUT)
            hello = recv_message(conn)
            if hello.get("type") != MSG_HELLO:
                raise ProtocolError("Expected a HELLO message")
            peer = self._peer_from_hello(hello, addr[0], conn)

            error = self._register_peer(peer)
            if error:
                send_message(conn, make_error(error))
                raise ProtocolError(error)

            send_message(conn, make_hello_ack(self.peer_id, self.name, self.port))
            conn.settimeout(None)
        except (ProtocolError, ConnectionClosed, OSError) as exc:
            if peer is not None:
                self._unregister_quietly(peer)
            self._close_quietly(conn)
            if self._running:
                self.log(f"[ERROR] Rejected incoming connection from {addr[0]}: {exc}")
            return

        self._announce_connected(peer, incoming=True)
        self._reader_loop(peer)

    # --------------------------------------------------------- client role
    def connect(self, ip, port):
        """
        Connect to another peer and do the HELLO handshake.
        Raises ValueError / ConnectionError with a readable message on failure.
        """
        if not self._running:
            raise ConnectionError("Start your peer first")

        ip = str(ip).strip()
        if ip.lower() == "localhost":
            ip = "127.0.0.1"
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError(f"Invalid IP address: '{ip}'")
        try:
            port = int(str(port).strip())
        except ValueError:
            raise ValueError("Port must be a number")
        if not (1 <= port <= 65535):
            raise ValueError("Port must be between 1 and 65535")

        try:
            sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
        except socket.timeout:
            raise ConnectionError("Connection timed out")
        except OSError as exc:
            raise ConnectionError(exc.strerror or str(exc))

        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            send_message(sock, make_hello(self.peer_id, self.name, self.port))
            reply = recv_message(sock)
            if reply.get("type") == MSG_ERROR:
                raise ConnectionError(reply.get("message", "Peer refused the connection"))
            if reply.get("type") != MSG_HELLO_ACK:
                raise ProtocolError("Expected a HELLO acknowledgement")
            peer = self._peer_from_hello(reply, ip, sock, listen_port=port)

            error = self._register_peer(peer)
            if error:
                raise ConnectionError(error)
            sock.settimeout(None)
        except ConnectionError:
            self._close_quietly(sock)
            raise
        except (ProtocolError, ConnectionClosed, OSError) as exc:
            self._close_quietly(sock)
            raise ConnectionError(f"Handshake failed: {exc}")

        self._announce_connected(peer, incoming=False)
        # The reader thread handles everything this peer sends from now on.
        threading.Thread(target=self._reader_loop, args=(peer,), daemon=True).start()
        return peer

    # ----------------------------------------------------- peer bookkeeping
    def _peer_from_hello(self, msg, ip, sock, listen_port=None):
        peer_id = msg.get("peer_id")
        name = msg.get("peer_name")
        port = listen_port if listen_port is not None else msg.get("port")
        if not isinstance(peer_id, str) or not peer_id or len(peer_id) > 64:
            raise ProtocolError("HELLO has an invalid peer_id")
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise ProtocolError("HELLO has an invalid peer_name")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ProtocolError("HELLO has an invalid port")
        return Peer(peer_id, name.strip(), ip, port, sock)

    def _register_peer(self, peer):
        """Add a peer. Returns an error string if it cannot be added."""
        with self._peers_lock:
            if peer.peer_id == self.peer_id:
                return "Cannot connect to yourself"
            if peer.peer_id in self._peers:
                return "Already connected to this peer"
            self._peers[peer.peer_id] = peer
        return None

    def _unregister_quietly(self, peer):
        with self._peers_lock:
            if self._peers.get(peer.peer_id) is peer:
                del self._peers[peer.peer_id]

    def _announce_connected(self, peer, incoming):
        if incoming:
            self.log(f"[SYSTEM] Connected to {peer.name}")
        else:
            self.log(f"[SYSTEM] Connected to {peer.name} ({peer.ip}:{peer.port})")
        self._peers_callback()

    def _remove_peer(self, peer, reason, announce=True):
        """Close and forget a peer. Safe to call more than once."""
        with self._peers_lock:
            existed = self._peers.get(peer.peer_id) is peer
            if existed:
                del self._peers[peer.peer_id]
        self._close_quietly(peer.sock)
        if existed and announce:
            self.log(f"[SYSTEM] {peer.name} disconnected ({reason})")
        if existed:
            self._peers_callback()

    @staticmethod
    def _close_quietly(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # -------------------------------------------------------------- receiving
    def _reader_loop(self, peer):
        """Runs in its own thread: read messages from one peer until it leaves."""
        try:
            while self._running:
                msg = recv_message(peer.sock)
                kind = msg["type"]
                if kind == MSG_TEXT:
                    text = str(msg.get("message", ""))
                    self.log(f"{peer.name} -> You: {text}")
                elif kind == MSG_FILE:
                    self._receive_file(peer, msg)
                else:
                    self.log(f"[ERROR] Unknown message type from {peer.name}: {kind}")
        except ConnectionClosed:
            self._remove_peer(peer, "connection closed")
        except ProtocolError as exc:
            self._remove_peer(peer, f"protocol error: {exc}")
        except OSError as exc:
            self._remove_peer(peer, exc.strerror or "connection lost")

    def _receive_file(self, peer, msg):
        """
        Read the raw file bytes that follow a 'file' message.
        The size from the metadata tells us exactly how many bytes to read.
        """
        filesize = msg.get("filesize")
        if not isinstance(filesize, int) or isinstance(filesize, bool) or filesize < 0:
            raise ProtocolError("Invalid file size")
        filename = self._safe_filename(msg.get("filename"))

        final_path = self._unique_path(filename)
        temp_path = final_path + ".part"
        received = 0
        try:
            with open(temp_path, "wb") as out:
                while received < filesize:
                    chunk = peer.sock.recv(min(CHUNK_SIZE, filesize - received))
                    if not chunk:
                        raise ConnectionClosed("Peer disconnected during file transfer")
                    out.write(chunk)
                    received += len(chunk)
            os.replace(temp_path, final_path)
        except BaseException:
            # Do not leave a half-written file behind.
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        self.log(f"{peer.name} -> You: File received: {os.path.basename(final_path)}")

    @staticmethod
    def _safe_filename(name):
        """Keep only the file name part so a peer cannot write outside downloads/."""
        name = str(name or "").replace("\\", "/")
        name = os.path.basename(name).strip().strip(".")
        for bad in '<>:"|?*\x00':
            name = name.replace(bad, "_")
        return name or "received_file"

    def _unique_path(self, filename):
        """downloads/photo.jpg, then photo_1.jpg, photo_2.jpg ... if it exists."""
        base, ext = os.path.splitext(filename)
        path = os.path.join(self.download_dir, filename)
        counter = 1
        while os.path.exists(path) or os.path.exists(path + ".part"):
            path = os.path.join(self.download_dir, f"{base}_{counter}{ext}")
            counter += 1
        return path

    # ---------------------------------------------------------------- sending
    def send_text(self, peer_id, message):
        peer = self._require_peer(peer_id)
        try:
            with peer.send_lock:
                send_message(peer.sock, make_text(self.peer_id, self.name, message))
        except OSError as exc:
            self._remove_peer(peer, "send failed")
            raise ConnectionError(f"Could not send to {peer.name}: {exc.strerror or exc}")
        self.log(f"You -> {peer.name}: {message}")

    def send_file(self, peer_id, path, progress_callback=None):
        """Send a file as metadata + raw bytes, in 64 KB chunks."""
        peer = self._require_peer(peer_id)
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f"File does not exist: {path}")
        filesize = os.path.getsize(path)
        filename = os.path.basename(path)

        try:
            with peer.send_lock:
                send_message(peer.sock, make_file(self.peer_id, self.name, filename, filesize))
                sent = 0
                with open(path, "rb") as src:
                    while True:
                        chunk = src.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        peer.sock.sendall(chunk)
                        sent += len(chunk)
                        if progress_callback:
                            progress_callback(sent, filesize)
        except OSError as exc:
            self._remove_peer(peer, "send failed")
            raise ConnectionError(f"Could not send file to {peer.name}: {exc.strerror or exc}")
        self.log(f"You -> {peer.name}: File sent: {filename}")

    def _require_peer(self, peer_id):
        if not self._running:
            raise ConnectionError("Start your peer first")
        peer = self.get_peer(peer_id)
        if peer is None:
            raise ConnectionError("That peer is not connected")
        return peer
