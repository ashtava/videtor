import asyncio, json, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    params = StdioServerParameters(command="/Users/paritoshsrivastava/workspace/videtor/.venv/bin/python", args=["-m", "videtor.mcp_server"], cwd="/")
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            t0 = time.time()
            res = await s.call_tool("rank_music", {"length_s": 25, "reveal_at_s": 8.4, "top": 3})
            o = json.loads(res.content[0].text)
            print(round(time.time() - t0, 1), "s", res.is_error, o["tracks_scanned"], [(t["name"][:30], t["bpm"], t["in_s"], t["drop_s"]) for t in o["tracks"]])
asyncio.run(main())
