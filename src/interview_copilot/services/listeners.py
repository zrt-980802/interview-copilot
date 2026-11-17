"""Run multiple HTTP listeners with one runtime owner and one signal handler."""

import asyncio
from contextlib import nullcontext
import signal
import socket


async def serve_listeners(desktop, mobile=None, *, host="127.0.0.1", port=8527,
                          mobile_host="0.0.0.0", mobile_port=8528, on_bound=None):
    import uvicorn

    class SharedServer(uvicorn.Server):
        def capture_signals(self):
            return nullcontext()

    specs = [(desktop, host, port)] + ([(mobile, mobile_host, mobile_port)] if mobile else [])
    sockets, servers, tasks, handlers = [], [], [], {}
    try:
        # Reserve every port before creating model, audio or history resources.
        for app, bind_host, bind_port in specs:
            family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sockets.append(sock)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((bind_host, bind_port))
            sock.listen(128)
            sock.setblocking(False)
            servers.append(SharedServer(uvicorn.Config(app, host=bind_host, port=bind_port, lifespan="off",
                                                        log_level="warning", timeout_graceful_shutdown=3)))
        if on_bound:
            on_bound([sock.getsockname()[1] for sock in sockets])

        def shutdown(*_):
            for server in servers:
                server.should_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, shutdown)
        async with desktop.router.lifespan_context(desktop):
            try:
                tasks = [asyncio.create_task(server.serve(sockets=[sock])) for server, sock in zip(servers, sockets)]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                shutdown()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for sig, previous in handlers.items():
            signal.signal(sig, previous)
        for sock in sockets:
            sock.close()
