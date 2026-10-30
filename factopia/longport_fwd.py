"""长桥行情 WS 的本地 CONNECT 转发器。

容器内直连 openapi-quote.longportapp.com 会遭遇 DNS 污染 + TLS 层 SNI 阻断，
而 longport SDK 的 WebSocket 不读任何代理环境变量。本模块在容器内监听 443，
把每条连接经 Clash 的 HTTP CONNECT 隧道转发到真实行情端点：

    SDK → 127.0.0.1:443（/etc/hosts 把域名固定到这里）→ CONNECT 经 Clash → 真实端点

TLS/SNI 由 SDK 端到端完成，转发器只搬运密文。由 compose.local.yml 的 runner command 后台启动。

用法：python -m factopia.longport_fwd
"""
import asyncio
import os

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 443
TARGET = "openapi-quote.longportapp.com:443"
PROXY_HOST = "host.docker.internal"
PROXY_PORT = int(os.environ.get("LP_FWD_PROXY_PORT", "7897"))


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_reader, client_writer):
    try:
        proxy_reader, proxy_writer = await asyncio.open_connection(PROXY_HOST, PROXY_PORT)
        proxy_writer.write(f"CONNECT {TARGET} HTTP/1.1\r\nHost: {TARGET}\r\n\r\n".encode())
        await proxy_writer.drain()
        resp = await proxy_reader.readline()
        if b" 200 " not in resp:
            raise ConnectionError(f"代理 CONNECT 失败: {resp!r}")
        while True:  # 读完剩余响应头
            line = await proxy_reader.readline()
            if line in (b"\r\n", b"", b"\n"):
                break
        await asyncio.gather(
            _pipe(client_reader, proxy_writer),
            _pipe(proxy_reader, client_writer),
        )
    except Exception as e:
        print(f"[lp_fwd] 连接处理失败: {e}", flush=True)
        try:
            client_writer.close()
        except Exception:
            pass


async def main():
    server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT)
    print(
        f"[lp_fwd] listening on {LISTEN_HOST}:{LISTEN_PORT} -> {PROXY_HOST}:{PROXY_PORT} CONNECT {TARGET}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
