"""TEST ONLY: a TCP proxy between a process and PostgreSQL that a test can cut and restore, to
show what happens when IncidentLens's own database becomes unreachable (spec §17), without
pausing the shared database container."""

from __future__ import annotations

import socket
import threading


class TcpProxy:
    def __init__(self, target_host: str, target_port: int):
        self.target = (target_host, target_port)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.open = threading.Event()
        self.open.set()
        self.connections: list[socket.socket] = []
        self.lock = threading.Lock()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if not self.open.is_set():
                client.close()
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=5)
                upstream.settimeout(None)  # the 5 s applies to connecting only; idle connections must stay open
            except OSError:
                client.close()
                continue
            with self.lock:
                self.connections += [client, upstream]
            for source, destination in ((client, upstream), (upstream, client)):
                threading.Thread(target=self.pipe, args=(source, destination), daemon=True).start()

    def pipe(self, source: socket.socket, destination: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                destination.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, destination):
                try:
                    sock.close()
                except OSError:
                    pass

    def cut(self) -> None:
        """Refuses new connections and drops every open one."""
        self.open.clear()
        with self.lock:
            for sock in self.connections:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                    sock.close()
                except OSError:
                    pass
            self.connections.clear()

    def restore(self) -> None:
        self.open.set()

    def close(self) -> None:
        self.cut()
        self.listener.close()
