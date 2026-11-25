import asyncio
from aiohttp import web
from src import config
from src.engine import Engine


def test_dsnp_routes_to_the_separate_engine(unused_tcp_port):
    seen=[]
    async def run():
        async def handler(request):
            seen.append((request.host, await request.json()))
            return web.json_response({"titles": []})
        app=web.Application();app.router.add_post('/api/list-titles',handler)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',unused_tcp_port);await site.start()
        old=config.UNSHACKLE_DSNP_API;config.UNSHACKLE_DSNP_API=f'http://127.0.0.1:{unused_tcp_port}/api'
        try: await Engine('http://127.0.0.1:1/api').list_titles('DSNP','entity-x')
        finally: config.UNSHACKLE_DSNP_API=old;await runner.cleanup()
    asyncio.run(run())
    assert seen and seen[0][1]['service']=='DSNP'


def test_non_dsnp_keeps_primary_engine():
    e=Engine('http://primary.invalid/api')
    assert e.base=='http://primary.invalid/api'
