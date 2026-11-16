"""Smoke test: starts sand3d-mcp over stdio with the official MCP client and calls every tool for real.

    python tests/smoke.py                      # runs `sand3d-mcp` from the current environment
    python tests/smoke.py uvx --from . sand3d-mcp   # any other launch command

Writes the contact sheet and the downloaded object to tests/out/.
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OUT = Path(__file__).resolve().parent / "out"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, info: str) -> None:
    results.append((name, ok, info))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {info}", flush=True)


def texts(res) -> str:
    return "\n".join(c.text for c in res.content if c.type == "text")


def images(res) -> list:
    return [c for c in res.content if c.type == "image"]


async def call(session, name: str, args: dict):
    t = time.time()
    res = await session.call_tool(name, args)
    return res, time.time() - t


async def main(cmd: list[str]) -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    params = StdioServerParameters(command=cmd[0], args=cmd[1:])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        check("initialize", init.serverInfo.name == "sand3d", f"server {init.serverInfo.name}, protocol {init.protocolVersion}")

        tools = (await s.list_tools()).tools
        names = sorted(t.name for t in tools)
        want = {"search_objects", "find_similar", "get_object", "download_object", "list_datasets", "sand3d_format_guide"}
        ro = {t.name: t.annotations.readOnlyHint for t in tools}
        check("list_tools", want <= set(names) and ro["download_object"] is False and ro["search_objects"] is True,
              f"{len(names)} tools {names}, readOnly {ro}")

        res, dt = await call(s, "search_objects", {"query": "red tractor"})
        txt, imgs = texts(res), images(res)
        sheet = OUT / "sheet-red-tractor.jpg"
        if imgs:
            sheet.write_bytes(base64.b64decode(imgs[0].data))
        ids = [ln.split("id ", 1)[1].strip() for ln in txt.splitlines() if ln.strip().startswith("id ")]
        check("search_objects", not res.isError and len(ids) == 12 and len(imgs) == 1 and "ractor" in txt,
              f"{len(ids)} results, {len(imgs)} image {imgs[0].mimeType if imgs else ''} "
              f"{sheet.stat().st_size if imgs else 0} bytes -> {sheet}, {dt:.1f}s; first: {txt.splitlines()[1] if ids else txt[:200]}")
        if not ids:
            return 1
        first = ids[0]

        res, dt = await call(s, "search_objects", {"query": "hinge", "match_words": True, "only_with_joints": True,
                                                   "limit": 3, "contact_sheet": False})
        check("search_objects words+joints, no sheet", not res.isError and not images(res) and "joints" in texts(res),
              f"{texts(res).splitlines()[0]}, {dt:.1f}s")

        res, dt = await call(s, "find_similar", {"object_id": first, "limit": 8})
        sim = [ln for ln in texts(res).splitlines() if ln.strip().startswith("id ")]
        imgs = images(res)
        if imgs:
            (OUT / "sheet-similar.jpg").write_bytes(base64.b64decode(imgs[0].data))
        check("find_similar", not res.isError and len(sim) == 8 and len(imgs) == 1,
              f"{len(sim)} results, image {len(base64.b64decode(imgs[0].data)) if imgs else 0} bytes, {dt:.1f}s")

        res, dt = await call(s, "get_object", {"object_id": first, "program_lines": 15})
        t = texts(res)
        check("get_object", not res.isError and "attribution" in t and "```python" in t and "license" in t,
              f"{len(t)} chars, {dt:.1f}s; " + " | ".join(t.splitlines()[:5]))

        res, dt = await call(s, "list_datasets", {})
        t = texts(res)
        ds = [ln.split()[0] for ln in t.splitlines() if ln and not ln.startswith(" ")]
        check("list_datasets", not res.isError and {"objaverse", "articraft", "cad"} <= set(ds), f"datasets {ds}, {dt:.1f}s")

        res, dt = await call(s, "list_datasets", {"dataset": "articraft"})
        check("list_datasets articraft", not res.isError and "families:" in texts(res), f"{len(texts(res))} chars, {dt:.1f}s")

        # an articulated object to see joints in the download
        res, _ = await call(s, "search_objects", {"query": "stand mixer", "dataset": "articraft", "limit": 1, "contact_sheet": False})
        art = [ln.split("id ", 1)[1].strip() for ln in texts(res).splitlines() if ln.strip().startswith("id ")]
        target = art[0] if art else first
        res, dt = await call(s, "download_object", {"object_id": target, "directory": str(OUT / "assets"),
                                                    "subfolder": "obj", "include_threejs_helper": True})
        d = OUT / "assets" / "obj"
        glb = (d / "model.glb").read_bytes() if (d / "model.glb").exists() else b""
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        files = sorted(p.name for p in d.iterdir()) if d.exists() else []
        ok = (not res.isError and glb[:4] == b"glTF" and bool(meta.get("attribution")) and "program.py" in files
              and "render.png" in files and "s3d-joints.js" in files)
        check("download_object", ok, f"{target}, files {files}, glb {len(glb)} bytes magic {glb[:4]!r}, "
              f"joints {len((meta.get('glb') or {}).get('joints', []))}, attribution {meta.get('attribution')!r}, {dt:.1f}s")
        print("    " + "\n    ".join(texts(res).splitlines()[-8:]))

        res, dt = await call(s, "download_object", {"object_id": first, "directory": "rel/../../x", "subfolder": "../evil"})
        check("download_object rejects bad subfolder", res.isError and "subfolder" in texts(res), texts(res))

        res, dt = await call(s, "get_object", {"object_id": "objaverse-doesnotexist000"})
        t = texts(res)
        check("get_object missing id", res.isError and "no object" in t and "Traceback" not in t, f"{t} ({dt:.1f}s)")

        res, dt = await call(s, "find_similar", {"object_id": "nope-123"})
        check("find_similar missing id", res.isError and "Traceback" not in texts(res), texts(res))

        res, dt = await call(s, "search_objects", {"query": "x", "dataset": "nosuch", "contact_sheet": False})
        check("search_objects bad dataset", res.isError and "Available" in texts(res), texts(res))

        res, _ = await call(s, "search_objects", {"query": "x", "limit": 500})
        check("limit cap 200", res.isError, texts(res)[:160].replace("\n", " "))

        res, _ = await call(s, "sand3d_format_guide", {})
        check("sand3d_format_guide", "s3d_joint" in texts(res) and "0.001" in texts(res), f"{len(texts(res))} chars")

        rl = await s.list_resources()
        body = await s.read_resource("sand3d://guide")
        check("resource sand3d://guide", any(str(x.uri) == "sand3d://guide" for x in rl.resources) and "Y is up" in body.contents[0].text,
              f"{len(body.contents[0].text)} chars")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f", FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or ["sand3d-mcp"])))
