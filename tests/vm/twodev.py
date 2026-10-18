import asyncio, json, sys, re, aiohttp
U = "http://127.0.0.1:8099"
async def main(ghid):
    async with aiohttp.ClientSession(cookies={"ghid": ghid}, cookie_jar=aiohttp.CookieJar(unsafe=True)) as a:
        await a.post(U + "/play", data={"mode": "fresh"}, allow_redirects=False)
        sid = a.cookie_jar.filter_cookies(__import__("yarl").URL(U))["hid"].value
        while (await (await a.get(U + "/ready?sid=" + sid)).json())["state"] != "ready":
            await asyncio.sleep(1)
        async with aiohttp.ClientSession(cookies={"ghid": ghid}, cookie_jar=aiohttp.CookieJar(unsafe=True)) as b:   # 2nd device: account only
            outs = {"A": "", "B": ""}
            async def term(sess, name, sent):
                ws = await sess.ws_connect(f"{U}/t/{sid}/ws", protocols=("tty",))
                await ws.send_str(json.dumps({"AuthToken": "", "columns": 100, "rows": 30}))
                async def reader():
                    async for m in ws:
                        d = m.data if m.type == aiohttp.WSMsgType.BINARY else m.data.encode()
                        if d[:1] == b"0":
                            outs[name] += d[1:].decode(errors="replace")
                asyncio.ensure_future(reader())
                return ws
            wa = await term(a, "A", True)
            await asyncio.sleep(4)
            wb = await term(b, "B", False)
            await asyncio.sleep(3)
            await wa.send_bytes(b"0" + b"echo shared-$((6*7)) as $(id -un)\r")
            await asyncio.sleep(3)
            clean = lambda t: re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r", "", t)
            print("A banner:", clean(outs["A"]).count("THE HANDOVER"), "| B saw A's command output:", "shared-42 as level0" in clean(outs["B"]))
            await wa.close(); await wb.close()
asyncio.run(main(sys.argv[1]))
