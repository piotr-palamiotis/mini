"""The egress forwarder lets a server-first protocol (SMTP) greet within a client's usual timeout."""
import importlib.util
import os
import socket
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location("egress", os.path.join(os.path.dirname(__file__), "..", "sandbox",
                                                                      "egress.py"))
egress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(egress)


class ServerFirstTest(unittest.TestCase):
    def test_the_greeting_arrives_quickly_with_one_host(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def greet():
            c, _ = srv.accept()
            c.sendall(b"220 ready\r\n")
            time.sleep(0.5)
            c.close()
        threading.Thread(target=greet, daemon=True).start()
        client, conn = socket.socketpair()
        egress.log = lambda **kw: None
        threading.Thread(target=egress.handle, args=(conn, port, ["127.0.0.1"]), daemon=True).start()
        client.settimeout(5)
        t0 = time.time()
        self.assertEqual(client.recv(64), b"220 ready\r\n")
        self.assertLess(time.time() - t0, 3)
        client.close()
        srv.close()


if __name__ == "__main__":
    unittest.main()
