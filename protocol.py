"""
protocol.py - Application-level protocol for the P2P assignment.

TCP is a byte stream, so we have to mark where one message ends and the next
begins (message framing). Every control message is sent as:

    [4-byte length][JSON payload]

The length is an unsigned 32-bit integer in network byte order (big-endian).
The receiver reads exactly 4 bytes, decodes the length, then reads exactly
that many bytes and parses them as JSON.

File transfer uses the same framing for the metadata message, and then the
raw file bytes follow immediately on the same connection:

    [4-byte length][{"type": "file", "filename": ..., "filesize": N}][N raw bytes]
"""

import json
import struct

# Message types
MSG_HELLO = "hello"
MSG_HELLO_ACK = "hello_ack"
MSG_TEXT = "text"
MSG_FILE = "file"
MSG_ERROR = "error"

HEADER_SIZE = 4                    # bytes used for the length prefix
MAX_MESSAGE_SIZE = 1024 * 1024     # 1 MB cap for JSON control messages
CHUNK_SIZE = 64 * 1024             # 64 KB chunks for file transfer


class ProtocolError(Exception):
    """The other side sent something that does not follow the protocol."""


class ConnectionClosed(Exception):
    """The other side closed the connection."""


def encode_message(message: dict) -> bytes:
    """Turn a dict into [4-byte length][JSON bytes]."""
    payload = json.dumps(message).encode("utf-8")
    if len(payload) > MAX_MESSAGE_SIZE:
        raise ProtocolError("Message too large")
    return struct.pack("!I", len(payload)) + payload


def send_message(sock, message: dict) -> None:
    """Frame and send one control message."""
    sock.sendall(encode_message(message))


def recv_exact(sock, count: int) -> bytes:
    """
    Read exactly `count` bytes from the socket.

    A single recv() may return fewer bytes than requested (TCP does not keep
    message boundaries), so we loop until we have everything.
    """
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionClosed("Connection closed by peer")
        data.extend(chunk)
    return bytes(data)


def recv_message(sock) -> dict:
    """Read one framed JSON message and return it as a dict."""
    header = recv_exact(sock, HEADER_SIZE)
    (length,) = struct.unpack("!I", header)
    if length == 0 or length > MAX_MESSAGE_SIZE:
        raise ProtocolError(f"Invalid message length: {length}")
    payload = recv_exact(sock, length)
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProtocolError("Message is not valid JSON")
    if not isinstance(message, dict) or "type" not in message:
        raise ProtocolError("Message has no 'type' field")
    return message


# ---- Message builders -------------------------------------------------------

def make_hello(peer_id: str, peer_name: str, port: int) -> dict:
    return {"type": MSG_HELLO, "peer_id": peer_id, "peer_name": peer_name, "port": port}


def make_hello_ack(peer_id: str, peer_name: str, port: int) -> dict:
    return {"type": MSG_HELLO_ACK, "peer_id": peer_id, "peer_name": peer_name, "port": port}


def make_text(sender_id: str, sender_name: str, message: str) -> dict:
    return {"type": MSG_TEXT, "sender_id": sender_id, "sender_name": sender_name, "message": message}


def make_file(sender_id: str, sender_name: str, filename: str, filesize: int) -> dict:
    return {
        "type": MSG_FILE,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "filename": filename,
        "filesize": filesize,
    }


def make_error(text: str) -> dict:
    return {"type": MSG_ERROR, "message": text}
