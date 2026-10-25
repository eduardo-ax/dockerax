"""Tiny backend: every request records a visit in postgres and returns JSON."""
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

DB = os.environ["DATABASE_URL"]

with psycopg.connect(DB) as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS visits (id serial PRIMARY KEY, at timestamptz DEFAULT now(), ip text)")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        ip = self.headers.get("X-Real-IP", self.client_address[0])
        with psycopg.connect(DB) as conn:
            conn.execute("INSERT INTO visits (ip) VALUES (%s)", (ip,))
            total = conn.execute("SELECT count(*) FROM visits").fetchone()[0]
            version = conn.execute("SELECT version()").fetchone()[0]
        body = json.dumps({
            "message": "hello from the backend",
            "backend_container": socket.gethostname(),
            "visits": total,
            "postgres": version.split(" on ")[0],
        }, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body + b"\n")


print("backend listening on :8000", flush=True)
ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
