import asyncio, json, sys, re, aiohttp
U = "http://127.0.0.1:8099"
clean = lambda t: re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r", "", t)


async def until(cond, secs):
    """Poll cond() (sync or async) every 0.25 s; False if it never holds within secs."""
    for _ in range(secs * 4):
        if await cond() if asyncio.iscoroutinefunction(cond) else cond():
            return True
        await asyncio.sleep(0.25)
    return False


async def main(ghid):
    async with aiohttp.ClientSession(cookies={"ghid": ghid}, cookie_jar=aiohttp.CookieJar(unsafe=True)) as a:
        await a.post(U + "/play", data={"mode": "fresh"}, allow_redirects=False)
        sid = a.cookie_jar.filter_cookies(__import__("yarl").URL(U))["hid"].value

        async def ready():
            return (await (await a.get(U + "/ready?sid=" + sid)).json())["state"] == "ready"
        if not await until(ready, 90):
            sys.exit("box never ready")
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
            if not await until(lambda: "$" in clean(outs["A"]), 30):        # A's shell prompt
                sys.exit("A never got a prompt")
            wb = await term(b, "B", False)
            if not await until(lambda: outs["B"], 30):                       # B attached to the screen
                sys.exit("B never saw the screen")
            await wa.send_bytes(b"0" + b"echo shared-$((6*7)) as $(id -un)\r")
            await until(lambda: "shared-42 as level0" in clean(outs["B"]), 15)
            print("A banner:", clean(outs["A"]).count("THE HANDOVER"), "| B saw A's command output:", "shared-42 as level0" in clean(outs["B"]))
            await wa.close(); await wb.close()
asyncio.run(main(sys.argv[1]))
