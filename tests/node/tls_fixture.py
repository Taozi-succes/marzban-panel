"""A local TLS 1.3 origin for WSS, REALITY's target, and HTTPS egress tests."""
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"isolated-egress-verified\n" + b"x" * 32768
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("0.0.0.0", 18443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/data/origin.crt", "/data/origin.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
