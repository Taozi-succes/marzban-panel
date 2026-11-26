"""Test-only SOCKS/TLS client; CA verification uses the fixture's explicit trust anchor."""
import json
import socket
import ssl
import struct
import subprocess
import sys
import time

config, target = json.loads(sys.stdin.readline()), sys.stdin.readline().strip()
port = config["inbounds"][0]["port"]
proc = subprocess.Popen(["/usr/local/bin/xray", "run", "-config", "stdin:"], stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
proc.stdin.write(json.dumps(config).encode())
proc.stdin.close()


def read_exact(connection, count):
    result = b""
    while len(result) < count:
        part = connection.recv(count - len(result))
        if not part:
            raise RuntimeError("Truncated SOCKS response")
        result += part
    return result


try:
    for _ in range(60):
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=1)
            break
        except OSError:
            time.sleep(.2)
    else:
        raise RuntimeError("Test client did not start")
    connection.settimeout(15)
    connection.sendall(b"\x05\x01\x00")
    assert read_exact(connection, 2) == b"\x05\x00"
    address = target.encode()
    connection.sendall(b"\x05\x01\x00\x03" + bytes([len(address)]) + address + struct.pack("!H", 18443))
    response = read_exact(connection, 4)
    assert response[:2] == b"\x05\x00"
    length = {1: 4, 4: 16}.get(response[3])
    if length is None:
        length = read_exact(connection, 1)[0]
    read_exact(connection, length + 2)
    context = ssl.create_default_context(cafile="/data/origin.crt")
    with context.wrap_socket(connection, server_hostname="ws.example.test") as tls:
        tls.sendall(b"GET /egress HTTP/1.1\r\nHost: ws.example.test\r\nConnection: close\r\n\r\n")
        body = b""
        while True:
            part = tls.recv(65536)
            if not part:
                break
            body += part
    assert b"200 OK" in body.split(b"\r\n", 1)[0] and b"isolated-egress-verified" in body
    print("authenticated proxy and verified HTTPS origin passed")
finally:
    proc.terminate()
    proc.wait(timeout=5)
