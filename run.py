"""程序入口。

用法：
    python run.py
    python run.py --host 0.0.0.0 --port 8000

Windows 注意事项：
    ib_insync 需要 SelectorEventLoop（Windows 默认是 ProactorEventLoop），
    且 nest_asyncio 必须补丁到 uvicorn 实际使用的同一个 loop 实例上。
    因此不使用 uvicorn.run()（内部会新建 loop），改用 loop.run_until_complete()。
"""

import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(description="IB Stop-Loss Bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # ── Windows 事件循环修复 ──────────────────────────────────────────────
    # 必须在 new_event_loop() 之前设置 policy，否则创建的仍是 ProactorEventLoop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 创建唯一的 SelectorEventLoop 并立刻用 nest_asyncio 打补丁
    # 补丁打在这个具体实例上，之后 uvicorn 和 ib_insync 共用同一个 loop
    import nest_asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    nest_asyncio.apply(loop)

    # nest_asyncio 将 server.serve() 包成 Task-1；Ctrl-C 时该 Task 以
    # KeyboardInterrupt 结束但异常未被取走，GC 时 asyncio 会打警告。
    # 自定义异常处理器把 KeyboardInterrupt / SystemExit 静默掉即可。
    def _exc_handler(lp, ctx):
        if isinstance(ctx.get('exception'), (KeyboardInterrupt, SystemExit)):
            return
        lp.default_exception_handler(ctx)

    loop.set_exception_handler(_exc_handler)

    # ── 启动 uvicorn（在上面已打补丁的 loop 内运行）──────────────────────
    import uvicorn
    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        loop.run_until_complete(server.serve())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        pending = asyncio.all_tasks(loop)
        if pending:
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    main()
