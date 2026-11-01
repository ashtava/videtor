import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SP = sys.argv[1]
async def main():
    params = StdioServerParameters(command="/Users/paritoshsrivastava/workspace/videtor/.venv/bin/python", args=["-m", "videtor.mcp_server"], cwd="/")
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print("server:", init.server_info.name, "| instructions:", (init.instructions or "")[:60])
            tools = await s.list_tools()
            print("tools:", [t.name for t in tools.tools])
            res = await s.call_tool("list_projects", {})
            print("list_projects:", json.loads(res.content[0].text)["kb_dir"])
            res = await s.call_tool("read_back", {"picks_path": f"{SP}/mcp-cut/picks.json", "xml_path": f"{SP}/mcp-cut/guru-25s-brief.xml", "save": False})
            o = json.loads(res.content[0].text); print("read_back:", res.is_error, o.get("summary"), o["slots"][:2])
            res = await s.call_tool("search_footage", {"key": "Guru.xml::Sequence 01", "query": "a man putting on sunglasses", "top": 2})
            print("search:", res.is_error, [h["take"] for h in json.loads(res.content[0].text)["hits"]])
asyncio.run(main())
