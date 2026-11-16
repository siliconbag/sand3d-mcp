"""sand3d-mcp, an MCP server (stdio) for the open Sand3D 3D object catalog by Sandflow.

Unofficial, not affiliated with Sandflow. Talks to the public API of
https://huggingface.co/spaces/Sandflow/sand3d-explorer
"""
from __future__ import annotations

import base64
import json
import logging
import sys
from importlib import resources
from pathlib import Path
from typing import Annotated, Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import client as s3d
from .client import ApiError

GUIDE = """\
# Sand3D GLB format and joints

Every Sand3D object is a short Python program on the `sand3d` library, exported to GLB.

## Units and axes
- Units are meters, Y is up. Sizes are real world (a stand mixer is about 0.49 m tall, a tower about 8 m).
  Scale to your scene with `fitToHeight(root, h)` from `s3d-joints.js` or by your own bounding box code.
- The root node carries scale 0.001 and the parts inside it are in millimeters. Do not reset that scale.
- Nodes are named like the parts in the program (`bowl_shell`, `head`, `speed_knob`).
  Find a part with `root.getObjectByName("head")` in three.js.
- No textures and no animations. Materials are flat PBR colors named like `plastic #ede0c7`.
  Some CAD and AutoMate objects have no materials at all (white), assign colors yourself.
- Files are light, usually 10 to 700 KB and up to about 30k triangles.

## Joints
- Joints live in `extras.s3d_joint` on separate pivot nodes. GLTFLoader in three.js copies extras to
  `node.userData.s3d_joint = { name, type, axis, lower, upper, value, mimic }`.
- Types are `revolute` (radians, limited), `continuous` (radians, no limits), `prismatic` (meters).
- `axis` is in the pivot node's own frame, already in GLB axes. `value` is the pose the file was baked with,
  it can be non-zero, so move relative to it.
- `mimic = { joint, multiplier }` means the joint follows another one.
- To move a joint, rotate (or translate) the pivot node itself; its children follow.
  For a prismatic joint the node position offset must be divided by the parent scale ratio, because the
  node sits inside the millimeter root.

## three.js
Call `download_object` with `include_threejs_helper=true` to get `s3d-joints.js` next to the model.

```js
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { collectJoints, setJoint, setJoint01, fitToHeight } from "./s3d-joints.js";

const gltf = await new GLTFLoader().loadAsync("assets/sand3d/mixer/model.glb");
const root = gltf.scene;
const joints = collectJoints(root);   // right after loading
fitToHeight(root, 1.2);               // 1.2 scene units tall, standing on the floor at the origin
scene.add(root);

const head = joints.find((j) => j.name === "base_to_head");
setJoint01(head, 0.5);                // half of the travel
setJoint(head, 0.3);                  // or an exact value in radians (clamped to limits, drives mimic joints)
// continuous joints: setJoint(wheel, t * speed) every frame
```

## The program
`program.py` is plain Python on Blender (`pip install sand3d`, needs Python 3.11 or 3.13 and the `bpy` package).
The entry point is in `meta.json` field `entry`. Export with `s3d.export(<entry>(), "out.glb")`
(glb, usda, urdf, obj, stl). Program units are millimeters with Z up. Even without running it, the program shows
the parameters (diameters, lengths, tooth counts) and part names.

## Licenses
The catalog is released under CC BY 4.0, sources keep their own licenses (`source_license`, `license_status`
in meta.json). When publishing a video, site or game with an object, credit it with the `attribution` string
from meta.json.

## Fair use of the service
The API runs on a single CPU space owned by Sandflow. Dozens of objects are fine. For bulk work use the open
datasets `Sandflow/sand3d-objaverse`, `-cad`, `-articraft`, `-simready`, `-automate` on Hugging Face
(parquet with id, family, program, render and a usda folder). Ready GLBs are not in the datasets, only the space
serves them.
"""

INSTRUCTIONS = """\
Search and download ready 3D objects (GLB plus the Python program that builds them) from the open Sand3D
catalog by Sandflow, about 100k objects under CC BY 4.0.
Workflow: translate the request to English and describe how the thing looks, call search_objects, look at the
numbered contact sheet image to pick (names repeat a lot), refine with find_similar, then download_object into
the user's project folder. For moving parts search the articraft dataset or set only_with_joints.
Read sand3d_format_guide before writing three.js or game code for a downloaded model.
The service is a single CPU space run by Sandflow: dozens of objects are fine, bulk downloads belong to the
open Hugging Face datasets Sandflow/sand3d-*.
"""

mcp = FastMCP("sand3d", instructions=INSTRUCTIONS, log_level="WARNING")

READ_NET = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
FAIR_USE = ("Runs against a free single-CPU space by Sandflow: fine for dozens of objects; for bulk work use the "
            "open Hugging Face datasets Sandflow/sand3d-*.")


def _text(t: str) -> TextContent:
    return TextContent(type="text", text=t)


def _error(msg: str) -> CallToolResult:
    return CallToolResult(content=[_text(f"Error: {msg}")], isError=True)


async def _run(fn: Callable[[], CallToolResult]) -> CallToolResult:
    """Run blocking network code in a thread and turn failures into readable tool errors."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except ApiError as e:
        return _error(str(e))
    except OSError as e:
        return _error(f"file system error: {e}")
    except Exception as e:  # noqa: BLE001 - never leak a traceback to the client
        logging.getLogger("sand3d_mcp").exception("unexpected error")
        return _error(f"unexpected {type(e).__name__}: {e}")


def _listing(cards: list[dict], header: str, contact_sheet: bool) -> CallToolResult:
    lines = [header] + [s3d.line(i, c) for i, c in enumerate(cards, 1)]
    content: list = []
    if cards and contact_sheet:
        img = s3d.sheet(cards, header)
        if img:
            extra = (f" Only the first {s3d.SHEET_MAX_ITEMS} are on the sheet." if len(cards) > s3d.SHEET_MAX_ITEMS else "")
            lines.append(f"\nContact sheet attached, numbers on the image match the list.{extra}")
            content.append(ImageContent(type="image", data=base64.b64encode(img).decode(), mimeType="image/jpeg"))
        else:
            lines.append("\nContact sheet unavailable (Pillow missing).")
    if not cards:
        lines.append("Nothing found. Try other English words, match_words=true, or drop filters.")
    return CallToolResult(content=[_text("\n".join(lines)), *content])


Limit = Annotated[int, Field(ge=1, le=s3d.MAX_LIMIT, description="How many results to return (1-200).")]
Sheet = Annotated[bool, Field(description="Attach a numbered contact sheet JPEG of the renders (about 100-380 KB).")]


@mcp.tool(annotations=READ_NET, description=(
    "Search the Sand3D catalog (about 100k 3D objects with GLB, render and generating Python program, CC BY 4.0). "
    "Default mode compares an English text description with the renders (CLIP), so describe the look in English, "
    "e.g. 'old red farm tractor'. match_words=true does exact keyword search in names, families, attribution and "
    "program code. Returns a numbered text list with object ids and a contact sheet image with the same numbers; "
    "names repeat a lot, so pick by the image. " + FAIR_USE))
async def search_objects(
    query: Annotated[str, Field(description="English description of the object, or keywords when match_words=true. Empty lists the catalog with filters.")] = "",
    match_words: Annotated[bool, Field(description="Exact keyword search instead of visual meaning search.")] = False,
    dataset: Annotated[str, Field(description="Limit to one dataset: objaverse, cad, articraft, simready, automate. See list_datasets.")] = "",
    family: Annotated[str, Field(description="Family key or label inside the dataset (needs dataset), e.g. 'musical instrument'.")] = "",
    only_with_joints: Annotated[bool, Field(description="Only articulated objects (doors, wheels, levers that move).")] = False,
    include_without_glb: Annotated[bool, Field(description="Also return objects that have no GLB (only USDA).")] = False,
    assembly_parts: Annotated[bool, Field(description="Search individual parts of AutoMate assemblies.")] = False,
    limit: Limit = 12,
    offset: Annotated[int, Field(ge=0, description="Skip this many results (pagination).")] = 0,
    contact_sheet: Sheet = True,
) -> CallToolResult:
    def work() -> CallToolResult:
        d = s3d.search(query.strip(), words=match_words, dataset=dataset.strip(), family=family.strip(),
                       joints=only_with_joints, include_no_glb=include_without_glb, parts=assembly_parts,
                       offset=offset, limit=limit)
        mode = {"meaning": "by meaning", "words": "by words", "all": "no query"}.get(d["mode"], d["mode"])
        found = f", {d['total']} matches" if d["mode"] != "meaning" else ""
        filt = ", ".join(x for x in (dataset and f"dataset {dataset}", d.get("family_key") and f"family {d['family_key']}",
                                     only_with_joints and "with joints") if x)
        header = f"'{query}' {mode}{found}{', ' + filt if filt else ''}, showing {len(d['items'])} from #{offset + 1}"
        res = _listing(d["items"], header, contact_sheet)
        if d["items"] and len(d["items"]) == limit:
            res.content[0].text += f"\nMore results: offset={offset + limit}."
        return res
    return await _run(work)


@mcp.tool(annotations=READ_NET, description=(
    "Find objects that look like a given Sand3D object (visual nearest neighbours). Returns a numbered list and a "
    "contact sheet image. " + FAIR_USE))
async def find_similar(
    object_id: Annotated[str, Field(description="Object id from search_objects, e.g. 'objaverse-d1147d567cf84d569c1abec10301cd29'.")],
    limit: Limit = 12,
    same_dataset: Annotated[bool, Field(description="Only look in the same dataset as the given object.")] = False,
    contact_sheet: Sheet = True,
) -> CallToolResult:
    def work() -> CallToolResult:
        d = s3d.similar(object_id, limit, same_dataset)
        if not d.get("items"):
            s3d.get(object_id)  # the API answers an unknown id with an empty list, turn that into a clear error
        return _listing(d["items"], f"similar to {object_id}", contact_sheet)
    return await _run(work)


@mcp.tool(annotations=READ_NET, description=(
    "Get the card of one Sand3D object: name, dataset, family, parts, joints, license, attribution string, page "
    "URL, and its generating Python program (sand3d library, millimeters, Z up). The program is cut to "
    "program_lines unless full_program=true."))
async def get_object(
    object_id: Annotated[str, Field(description="Object id from search_objects or find_similar.")],
    program_lines: Annotated[int, Field(ge=0, le=2000, description="How many program lines to include.")] = 60,
    full_program: Annotated[bool, Field(description="Return the whole program.")] = False,
) -> CallToolResult:
    def work() -> CallToolResult:
        o = s3d.get(object_id)
        out = [s3d.line(1, o)[4:],
               f"dataset {o.get('dataset_label') or o.get('dataset')}, family {o.get('family')}, source {o.get('source_label')}",
               f"has GLB {bool(o.get('glb_url'))}, F-score {o.get('f_score')}, sand3d {o.get('sand3d_version')}, entry point {o.get('entry')}",
               f"license {o.get('license')} (source license {o.get('source_license')}, status {o.get('license_status')})",
               f"attribution {o.get('attribution')}",
               f"page {s3d.page_url(o['id'])}", ""]
        code = (o.get("program") or "").splitlines()
        shown = code if full_program else code[:program_lines]
        out.append(f"program.py ({len(code)} lines):\n```python\n" + "\n".join(shown) + "\n```")
        if len(shown) < len(code):
            out.append(f"... {len(code) - len(shown)} more lines, use full_program=true or download_object.")
        return CallToolResult(content=[_text("\n".join(out))])
    return await _run(work)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
          description=(
    "Download one Sand3D object into a folder the user chose: model.glb, program.py, render.png (512x512, "
    "transparent) and meta.json with license and attribution; model.usda too when include_usda=true or when the "
    "object has no GLB. Files go to <directory>/<subfolder>; existing files with the same names are overwritten. "
    "Returns paths, sizes, GLB stats (meters, Y up), part names, joints and the attribution string to credit (CC BY 4.0). "
    "include_threejs_helper=true also writes s3d-joints.js (collectJoints, setJoint, setJoint01, fitToHeight). "
    "Only writes inside the given directory. " + FAIR_USE))
async def download_object(
    object_id: Annotated[str, Field(description="Object id from search_objects or find_similar.")],
    directory: Annotated[str, Field(description="Target root folder, absolute path or relative to the client's working folder, e.g. 'assets/sand3d'. Ask the user if unsure.")],
    subfolder: Annotated[str, Field(description="Subfolder name inside directory. Default is '<name-slug>-<hash>' from the object name.")] = "",
    include_usda: Annotated[bool, Field(description="Also download model.usda from the Hugging Face dataset.")] = False,
    include_threejs_helper: Annotated[bool, Field(description="Also write s3d-joints.js, a three.js helper for joints and scaling.")] = False,
) -> CallToolResult:
    def work() -> CallToolResult:
        if not directory.strip():
            raise ApiError("directory is required")
        root = Path(directory.strip()).expanduser()
        if not root.is_absolute():
            cwd = Path.cwd()
            if cwd == Path(cwd.anchor):
                raise ApiError(f"the server runs in '{cwd}', pass an absolute directory path instead of '{directory}'")
            root = cwd / root
        name = subfolder.strip() or None
        if name and (Path(name).is_absolute() or ".." in Path(name).parts):
            raise ApiError("subfolder must be a plain folder name inside directory")
        o = s3d.get(object_id)
        dest = (root / (name or s3d.slug(o["name"], o["id"]))).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        (dest / "program.py").write_text(o.get("program") or "", encoding="utf-8")
        files["program"] = "program.py"
        (dest / "render.png").write_bytes(s3d.render_png(o["id"]))
        files["render"] = "render.png"
        info = None
        if o.get("glb_url"):
            blob = s3d.fetch(o["glb_url"].split("?")[0], timeout=180)
            info = s3d.glb_info(blob)
            (dest / "model.glb").write_bytes(blob)
            files["glb"] = "model.glb"
        usda_raw = (o.get("usda_url") or "").replace("/blob/", "/resolve/")
        if usda_raw and (include_usda or not o.get("glb_url")):
            (dest / "model.usda").write_bytes(s3d.fetch(usda_raw, base="", timeout=180))
            files["usda"] = "model.usda"
        if include_threejs_helper:
            js = resources.files("sand3d_mcp").joinpath("assets/s3d-joints.js").read_bytes()
            (dest / "s3d-joints.js").write_bytes(js)
            files["threejs_helper"] = "s3d-joints.js"
        meta = {k: o.get(k) for k in ("id", "name", "dataset", "dataset_label", "family", "family_raw", "source_label", "entry",
                                      "joints", "parts", "loc", "f_score", "license", "source_license", "license_status",
                                      "attribution", "sand3d_version", "asset_id", "repo", "row_url")}
        meta.update(page=s3d.page_url(o["id"]), usda_download=usda_raw, files=files, glb=info)
        (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

        out = [f"Saved '{o['name']}' ({o['id']}) to {dest}"]
        for f in [*files.values(), "meta.json"]:
            out.append(f"  {dest / f}  {(dest / f).stat().st_size:,} bytes")
        if info:
            sz = info["size"]
            out.append(f"GLB: {info['meshes']} meshes, {info['triangles']:,} triangles, {len(info['materials'])} materials")
            out.append(f"size {sz[0]:.4g} x {sz[1]:.4g} x {sz[2]:.4g} m (X x Y x Z, Y up), center {[round(v, 4) for v in info['center']]}")
            out.append("parts: " + (", ".join(p for p in info["parts"] if p) or "none named"))
            if info["joints"]:
                out.append("joints:")
                for j in info["joints"]:
                    lim = f", limits {j.get('lower')} .. {j.get('upper')}" if "lower" in j else ""
                    mim = f", mimics {j['mimic']}" if j.get("mimic") else ""
                    out.append(f"  {j.get('name')} on node {j.get('node')}: {j.get('type')}, axis {j.get('axis')}{lim}, baked value {j.get('value', 0)}{mim}")
            else:
                out.append("joints: none")
        else:
            out.append("No GLB was built for this object, saved USDA instead.")
        out.append(f"Attribution (CC BY 4.0, credit when publishing): {o.get('attribution')}")
        out.append("See sand3d_format_guide for units, root scale and moving joints.")
        return CallToolResult(content=[_text("\n".join(out))])
    return await _run(work)


@mcp.tool(annotations=READ_NET, description=(
    "List Sand3D datasets (objaverse household items, cad mechanical parts, articraft articulated objects, simready "
    "warehouse assets, automate mechanical assemblies) with object counts and their families. Pass dataset to see "
    "all families of one dataset; family keys or labels can be used in search_objects."))
async def list_datasets(
    dataset: Annotated[str, Field(description="Show all families of this dataset only.")] = "",
) -> CallToolResult:
    def work() -> CallToolResult:
        fac = s3d.facets()
        want = dataset.strip()
        if want and want not in {d["key"] for d in fac["datasets"]}:
            raise ApiError(f"no dataset '{want}'. Available: {', '.join(d['key'] for d in fac['datasets'])}")
        out = []
        for d in fac["datasets"]:
            if want and d["key"] != want:
                continue
            out.append(f"{d['key']}  {d['label']}: {d['count']} objects, {d['articulated']} with joints, {d['glb']} with GLB"
                       + (f", {d['parts']} assembly parts" if d.get("parts") else ""))
            out.append(f"    {d.get('blurb', '')}")
            fams = d["families"] if want else d["families"][:12]
            out.append("    families: " + ", ".join(f"{label} [{key}] {n}" if want else f"{label} {n}" for key, label, n in fams)
                       + ("" if want or len(d["families"]) <= 12 else f" ... {len(d['families'])} in total, pass dataset to see all"))
        return CallToolResult(content=[_text("\n".join(out))])
    return await _run(work)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
          description="How Sand3D GLB files are built (meters, Y up, root scale 0.001, part names, materials), how joints are "
                      "stored in extras.s3d_joint and how to move them in three.js, the program format, licenses and fair use. "
                      "Read before writing code that loads a downloaded model.")
async def sand3d_format_guide() -> CallToolResult:
    return CallToolResult(content=[_text(GUIDE)])


@mcp.resource("sand3d://guide", name="sand3d-format-guide", mime_type="text/markdown",
              description="Sand3D GLB format, joints, three.js usage, licenses and fair use.")
def guide_resource() -> str:
    return GUIDE


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="sand3d-mcp %(levelname)s %(message)s")
    mcp.run("stdio")


if __name__ == "__main__":
    main()
