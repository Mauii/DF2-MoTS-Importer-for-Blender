bl_info = {
    "name": "DF2 GOB 3DO Importer",
    "author": "Codex (uses gob.py, mat.py, 3do.py)",
    "version": (0, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > DF2 Importer",
    "description": "Import Dark Forces II / Jedi Knight 3DO models from a GOB/GOO archive",
    "category": "Import-Export",
}

import importlib.util
import logging
import math
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Euler, Matrix, Vector

from .gob import GOB


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger(__name__)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[DF2] %(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


log = _setup_logger()
_MODEL_DISPLAY_MAP: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# Backend loading helpers
# ---------------------------------------------------------------------------

def _load_three_do_module():
    """
    3do.py has a non-importable name, so load it manually and reuse the module.
    """
    module_name = "_df2_three_do"
    if module_name in sys.modules:
        return sys.modules[module_name]

    mod_path = Path(__file__).with_name("3do.py")
    spec = importlib.util.spec_from_file_location(module_name, mod_path)
    if spec is None or spec.loader is None:
        raise ImportError("Could not load 3do.py parser")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    log.info("Loaded 3DO parser from %s", mod_path)
    return module


three_do = _load_three_do_module()
ThreeDOParser = three_do.ThreeDOParser
ThreeDO = three_do.ThreeDO
MaterialDef = three_do.Material


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_cmp_from_gob(gob: GOB) -> Optional[List[int]]:
    """
    Find the first CMP palette in the archive and return a 768-int RGB list.
    """
    for entry in gob.list_entries():
        if entry.name.lower().endswith(".cmp"):
            data = gob.get_data(entry)
            # match viewer_gui.py palette logic
            if len(data) >= 0x340:
                pal = data[0x40:0x340]
            elif len(data) >= 0x300:
                pal = data[:0x300]
            else:
                continue
            if len(pal) >= 0x300:
                log.info("Using CMP palette %s", entry.name)
                return list(pal[:0x300])
    return None


def _find_entry_by_basename(gob: GOB, name: str):
    """
    Find an entry by exact name or by matching basename (case-insensitive).
    """
    entry = gob.find(name)
    if entry:
        return entry
    base = Path(name).name.lower()
    for e in gob.list_entries():
        if Path(e.name).name.lower() == base:
            return e
    return None


def _load_model_display_map(gob: GOB) -> Dict[str, str]:
    """
    Parse models.dat (if present) to map 3DO filenames to display names.
    """
    entry = _find_entry_by_basename(gob, "models.dat")
    if entry is None:
        return {}
    try:
        data = gob.get_data(entry).decode("ascii", errors="ignore")
    except Exception:
        return {}

    mapping: Dict[str, str] = {}
    for line in data.splitlines():
        if "#" not in line:
            continue
        # Look for: ky.3do ... #"Katarn"
        parts = line.split("#", 1)
        left = parts[0]
        right = parts[1]
        name = None
        if '"' in right:
            name = right.split('"')[1].strip()
        tokens = left.replace("\t", " ").split()
        three = None
        for tok in tokens:
            if tok.lower().endswith(".3do"):
                three = tok
                break
        if three and name:
            mapping[three.lower()] = name
    return mapping


def _decode_mat_first_frame(data: bytes, palette: Optional[List[int]]) -> Tuple[int, int, List[float], bool]:
    """
    Decode the top-level frame of a MAT to RGBA floats.
    Returns (width, height, pixels_float, has_alpha).
    """
    if data[:5] != b"MAT 2":
        raise ValueError("Not a MAT 2 file")

    mat_type = struct.unpack_from("<I", data, 8)[0]
    record_count = struct.unpack_from("<I", data, 12)[0]
    bit_depth = struct.unpack_from("<I", data, 24)[0]

    blue_bits = struct.unpack_from("<I", data, 28)[0]
    green_bits = struct.unpack_from("<I", data, 32)[0]
    red_bits = struct.unpack_from("<I", data, 36)[0]

    red_shift = struct.unpack_from("<I", data, 40)[0]
    green_shift = struct.unpack_from("<I", data, 44)[0]
    blue_shift = struct.unpack_from("<I", data, 48)[0]

    bpp = 1 if bit_depth == 8 else 2 if bit_depth == 16 else None
    if bpp is None:
        raise ValueError(f"Unsupported MAT bit depth {bit_depth}")

    offset = 76 + record_count * 40
    if offset + 24 > len(data):
        raise ValueError("MAT frame header truncated")

    width, height, trans_idx, _u0, _u1, num_mipmaps = struct.unpack_from("<6I", data, offset)
    offset += 24

    total_pixels = 0
    w, h = width, height
    for _ in range(max(1, num_mipmaps)):
        total_pixels += max(1, w) * max(1, h)
        if w <= 1 and h <= 1:
            break
        w = max(1, w // 2)
        h = max(1, h // 2)
    total_bytes = total_pixels * bpp
    if offset + total_bytes > len(data):
        total_bytes = max(0, len(data) - offset)

    top_bytes = width * height * bpp
    top_data = data[offset : offset + top_bytes]
    if len(top_data) < top_bytes:
        raise ValueError("MAT image data truncated")

    pixels: List[float] = []
    has_alpha = False

    if bit_depth == 8:
        if palette is None or len(palette) < 256 * 3:
            raise ValueError("Palette required for 8-bit MAT")
        for val in top_data:
            idx = val
            r = palette[idx * 3] / 255.0
            g = palette[idx * 3 + 1] / 255.0
            b = palette[idx * 3 + 2] / 255.0
            a = 1.0
            if trans_idx < 256 and idx == trans_idx:
                a = 0.0
                has_alpha = True
            pixels.extend([r, g, b, a])
    else:
        mask_r = (1 << red_bits) - 1 if red_bits else 0
        mask_g = (1 << green_bits) - 1 if green_bits else 0
        mask_b = (1 << blue_bits) - 1 if blue_bits else 0
        for i in range(0, len(top_data), 2):
            val = struct.unpack_from("<H", top_data, i)[0]
            r_raw = (val >> red_shift) & mask_r
            g_raw = (val >> green_shift) & mask_g
            b_raw = (val >> blue_shift) & mask_b
            r = int(r_raw * 255 / mask_r) if mask_r else 0
            g = int(g_raw * 255 / mask_g) if mask_g else 0
            b = int(b_raw * 255 / mask_b) if mask_b else 0
            pixels.extend([r / 255.0, g / 255.0, b / 255.0, 1.0])

    return width, height, pixels, has_alpha


def _calc_normals_safe(mesh_data: bpy.types.Mesh) -> None:
    for fn in ("calc_normals_split", "calc_normals"):
        if hasattr(mesh_data, fn):
            try:
                getattr(mesh_data, fn)()
                return
            except Exception:
                continue
    log.info("Mesh normal recalculation not available; continuing")


def _parse_3do_from_gob(gob: GOB, name: str) -> ThreeDO:
    entry = gob.find(name)
    if entry is None:
        raise FileNotFoundError(f"{name} not found in archive")
    text = gob.get_data(entry).decode("ascii", errors="ignore")
    log.info("Parsing 3DO %s", name)
    parser = ThreeDOParser(text)
    return parser.parse(Path(name))


# ---------------------------------------------------------------------------
# Blender helpers
# ---------------------------------------------------------------------------

def _ensure_materials(gob: GOB, materials: List[MaterialDef], palette: Optional[List[int]], tex_dir: Optional[Path]) -> Dict[int, bpy.types.Material]:
    mat_cache: Dict[int, bpy.types.Material] = {}
    for mat_def in materials:
        mat_name = Path(mat_def.name).name
        existing = bpy.data.materials.get(mat_name)
        if existing:
            mat_cache[mat_def.index] = existing
            log.info("Reusing existing material %s", mat_name)
            continue

        created = _build_material_from_mat(gob, mat_name, palette, tex_dir)
        mat_cache[mat_def.index] = created
        log.info("Built material %s", mat_name)
    return mat_cache


def _build_material_from_mat(gob: GOB, mat_name: str, palette: Optional[List[int]], tex_dir: Optional[Path]) -> bpy.types.Material:
    entry = _find_entry_by_basename(gob, mat_name)
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    links.clear()
    bsdf = nodes.get("Principled BSDF") or nodes.new("ShaderNodeBsdfPrincipled")
    output = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")

    if entry is None:
        # Fallback flat material
        bsdf.inputs["Base Color"].default_value = (0.8, 0.1, 0.1, 1.0)
        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
        log.warning("MAT %s not found in GOB, using flat color", mat_name)
        return mat

    try:
        data = gob.get_data(entry)
        width, height, pixels, has_alpha = _decode_mat_first_frame(data, palette)
        image = bpy.data.images.new(mat_name, width=width, height=height, alpha=True)
        image["df2_mat"] = mat_name
        image.pixels[:] = pixels
        if tex_dir:
            try:
                tex_dir.mkdir(parents=True, exist_ok=True)
                out_path = tex_dir / f"{Path(mat_name).stem}.png"
                image.filepath_raw = str(out_path)
                image.file_format = "PNG"
                image.save()
                log.info("Saved texture %s", out_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to save texture for %s: %s", mat_name, exc)
                image.pack()
        else:
            image.pack()
        image.colorspace_settings.name = "sRGB"

        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.image = image
        tex_node.interpolation = "Closest"
        tex_node.label = mat_name

        links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
        if has_alpha:
            links.new(tex_node.outputs["Alpha"], bsdf.inputs["Alpha"])
            mat.blend_method = "CLIP"

        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
        return mat
    except Exception as exc:  # noqa: BLE001
        # Leave a flat material as a safe fallback
        bsdf.inputs["Base Color"].default_value = (0.5, 0.5, 0.5, 1.0)
        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
        print(f"[DF2] Failed to decode MAT {mat_name}: {exc}")
        return mat


def _create_mesh_object(mesh_def, material_map: Dict[int, bpy.types.Material], texverts: List[three_do.TexVertex], palette_sizes: Dict[int, Tuple[int, int]]) -> bpy.types.Object:
    verts = [(v.x, v.y, v.z) for v in mesh_def.vertices]
    faces = [f.vertex_indices for f in mesh_def.faces]

    mesh_data = bpy.data.meshes.new(mesh_def.name or "3DO_Mesh")
    mesh_data.from_pydata(verts, [], faces)
    mesh_data.update()

    slot_lookup: Dict[int, int] = {}
    for slot_idx, mat_idx in enumerate(sorted(material_map.keys())):
        mesh_data.materials.append(material_map[mat_idx])
        slot_lookup[mat_idx] = slot_idx

    if mesh_def.faces:
        uv_layer = mesh_data.uv_layers.new(name="UVMap")
        for poly, face in zip(mesh_data.polygons, mesh_def.faces):
            mat_slot = material_map.get(face.material)
            if mat_slot:
                poly.material_index = slot_lookup.get(face.material, 0)
            width, height = palette_sizes.get(face.material, (256, 256))
            w = width if width else 256
            h = height if height else 256
            for loop_idx, t_idx in zip(poly.loop_indices, face.texvertex_indices):
                if t_idx < 0 or t_idx >= len(texverts):
                    continue
                tv = texverts[t_idx]
                u = tv.u / w
                v = tv.v / h  # engine uses origin at top; keep as-is to avoid flipping
                uv_layer.data[loop_idx].uv = (u, v)

    obj = bpy.data.objects.new(mesh_def.name or "3DO_Object", mesh_data)
    _calc_normals_safe(mesh_data)
    return obj


def _build_objects(
    context,
    gob: GOB,
    three: ThreeDO,
    palette: Optional[List[int]],
    tex_dir: Optional[Path],
    geoset_mode: str = "ALL",
) -> List[bpy.types.Object]:
    material_map = _ensure_materials(gob, three.materials, palette, tex_dir)

    def _build_palette_sizes(materials: List[Material]) -> Dict[int, Tuple[int, int]]:
        sizes: Dict[int, Tuple[int, int]] = {}
        for mat_def in materials:
            entry = _find_entry_by_basename(gob, Path(mat_def.name).name)
            if entry:
                try:
                    data = gob.get_data(entry)
                    w, h, _, _ = _decode_mat_first_frame(data, palette)
                    sizes[mat_def.index] = (w, h)
                except Exception:
                    sizes[mat_def.index] = (256, 256)
            else:
                sizes[mat_def.index] = (256, 256)
        return sizes

    palette_sizes = _build_palette_sizes(three.materials)

    # Coordinate conversion matrix (identity; swap Y/Z removed)
    conv_total = Matrix.Identity(4)

    created: List[bpy.types.Object] = []
    parent = bpy.data.objects.new(three.path.stem, None)
    context.collection.objects.link(parent)
    # Apply insert offset from file (object space) to the parent container
    if three.insert_offset:
        ins = Vector(three.insert_offset)
        ins_conv = conv_total @ ins.to_4d()
        parent.location = ins_conv.xyz

    geosets_iter = three.geosets
    if geoset_mode == "TOP" and three.geosets:
        geosets_iter = [three.geosets[0]]

    # Prepare hierarchy transforms without creating empties
    node_by_idx: Dict[int, HierarchyNode] = {n.index: n for n in three.hierarchy_nodes}
    node_world: Dict[int, Matrix] = {}

    def _node_world_matrix(idx: int) -> Matrix:
        if idx in node_world:
            return node_world[idx]
        node = node_by_idx.get(idx)
        if node is None:
            node_world[idx] = Matrix.Identity(4)
            return node_world[idx]
        parent_mat = Matrix.Identity(4)
        parent_pivot = None
        p_idx = node.parent_index
        if p_idx is not None and p_idx != idx:
            parent_mat = _node_world_matrix(p_idx)
            p_node = node_by_idx.get(p_idx)
            if p_node and p_node.pivot:
                parent_pivot = Vector(p_node.pivot)

        # Convert vectors through axis conversions
        pos = Vector(node.position) if node.position else Vector((0.0, 0.0, 0.0))
        pos_conv = (conv_total @ pos.to_4d()).xyz
        pivot = Vector(node.pivot) if node.pivot else Vector((0.0, 0.0, 0.0))
        pivot_conv = (conv_total @ pivot.to_4d()).xyz
        parent_pivot_conv = Vector((0.0, 0.0, 0.0))
        if parent_pivot:
            parent_pivot_conv = (conv_total @ parent_pivot.to_4d()).xyz

        rot_mat = Matrix.Identity(4)
        if node.rotation:
            # Engine uses intrinsic XYZ; do not permute with rot_order here.
            src_euler = Euler(
                (
                    math.radians(node.rotation[0]),
                    math.radians(node.rotation[1]),
                    math.radians(node.rotation[2]),
                ),
                "XYZ",
            )
            src_mat = src_euler.to_matrix().to_4x4()
            rot_mat = conv_total @ src_mat @ conv_total.inverted()

        # Game order: parent * T(pivot) * R * T(pos) * T(-parent_pivot)
        local = (
            Matrix.Translation(pivot_conv)
            @ rot_mat
            @ Matrix.Translation(pos_conv)
            @ Matrix.Translation(-parent_pivot_conv)
        )
        node_world[idx] = parent_mat @ local
        return node_world[idx]

    mesh_objects: Dict[int, bpy.types.Object] = {}
    for geoset in geosets_iter:
        for mesh_def in geoset.meshes:
            obj = _create_mesh_object(mesh_def, material_map, mesh_def.texvertices, palette_sizes)
            context.collection.objects.link(obj)
            obj.parent = parent
            mesh_objects[mesh_def.index] = obj
            created.append(obj)
            log.info("Created mesh object %s (verts=%d faces=%d)", obj.name, len(mesh_def.vertices), len(mesh_def.faces))

    # Apply hierarchy transforms directly to meshes (no empties)
    for node in three.hierarchy_nodes:
        mesh_idx = node.mesh_index
        if mesh_idx is None:
            continue
        mobj = mesh_objects.get(mesh_idx)
        if mobj is None:
            continue
        base_mat = _node_world_matrix(node.index)
        mobj.parent = parent
        mobj.parent_type = "OBJECT"
        mobj.matrix_parent_inverse.identity()
        # Include parent (insert offset) in world placement
        mobj.matrix_world = parent.matrix_world @ base_mat

    # Special case: ky* player models get fistg.3do attached to right forearm
    if three.path.name.lower().startswith("ky"):
        fist_entry = _find_entry_by_basename(gob, "fistg.3do")
        if fist_entry:
            try:
                fist_three = _parse_3do_from_gob(gob, fist_entry.name)
                fist_mat_map = _ensure_materials(gob, fist_three.materials, palette, tex_dir)
                fist_sizes = _build_palette_sizes(fist_three.materials)
                # Use highest LOD only for fist
                if fist_three.geosets:
                    fist_geoset = fist_three.geosets[0]
                    # Find right forearm node matrix from main model
                    forearm_node = next((n for n in three.hierarchy_nodes if (n.name or "").lower() == "k_rforearm"), None)
                    forearm_mat = Matrix.Identity(4)
                    if forearm_node:
                        forearm_mat = _node_world_matrix(forearm_node.index)
                    for mesh_def in fist_geoset.meshes:
                        obj = _create_mesh_object(mesh_def, fist_mat_map, mesh_def.texvertices, fist_sizes)
                        context.collection.objects.link(obj)
                        obj.parent = parent
                        obj.parent_type = "OBJECT"
                        obj.matrix_parent_inverse.identity()
                        obj.matrix_world = parent.matrix_world @ forearm_mat
                        created.append(obj)
                        log.info("Attached fist mesh %s to k_rforearm", obj.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to attach fistg.3do: %s", exc)
    return created


# ---------------------------------------------------------------------------
# UI classes
# ---------------------------------------------------------------------------

class DF2ModelItem(PropertyGroup):
    name: StringProperty(name="3DO Name")
    display_name: StringProperty(name="Display Name")


class DF2_OT_load_gob(Operator):
    bl_idname = "df2.load_gob"
    bl_label = "Open GOB/GOO"
    bl_description = "Select a Dark Forces II GOB/GOO archive"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.gob;*.goo", options={"HIDDEN"})

    def execute(self, context):
        global _MODEL_DISPLAY_MAP
        scene = context.scene
        try:
            gob = GOB(self.filepath)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Failed to read GOB: {exc}")
            return {"CANCELLED"}

        _MODEL_DISPLAY_MAP = _load_model_display_map(gob)
        scene.df2_gob_path = self.filepath
        scene.df2_models.clear()
        for entry in gob.list_entries():
            if entry.name.lower().endswith(".3do"):
                item = scene.df2_models.add()
                item.name = entry.name
                disp = _MODEL_DISPLAY_MAP.get(entry.name.lower(), entry.name)
                item.display_name = disp
        scene.df2_models_index = 0
        log.info("Loaded GOB %s with %d 3DO(s)", self.filepath, len(scene.df2_models))
        self.report({"INFO"}, f"Loaded {len(scene.df2_models)} model(s)")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class DF2_OT_import_model(Operator):
    bl_idname = "df2.import_model"
    bl_label = "Import Selected 3DO"
    bl_description = "Import the selected 3DO from the current GOB/GOO"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        if not scene.df2_gob_path:
            self.report({"ERROR"}, "Load a GOB first")
            return {"CANCELLED"}
        if scene.df2_models_index < 0 or scene.df2_models_index >= len(scene.df2_models):
            self.report({"ERROR"}, "No 3DO selected")
            return {"CANCELLED"}

        gob_path = scene.df2_gob_path
        model_name = scene.df2_models[scene.df2_models_index].name
        tex_dir = Path(scene.df2_tex_dir).expanduser() if scene.df2_tex_dir else None
        geoset_mode = "TOP" if scene.df2_geoset_top else "ALL"

        try:
            gob = GOB(gob_path)
            palette = _load_cmp_from_gob(gob)
            three = _parse_3do_from_gob(gob, model_name)
            created = _build_objects(context, gob, three, palette, tex_dir, geoset_mode)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Import failed: {exc}")
            return {"CANCELLED"}

        log.info("Imported %d object(s) from %s", len(created), model_name)
        self.report({"INFO"}, f"Imported {len(created)} mesh(es) from {model_name}")
        return {"FINISHED"}


class DF2_OT_save_textures(Operator):
    bl_idname = "df2.save_textures"
    bl_label = "Save DF2 Textures"
    bl_description = "Save imported DF2 textures to PNG files"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        scene = context.scene
        dir_path = self.directory or scene.df2_tex_dir
        if not dir_path:
            self.report({"ERROR"}, "Set a texture output directory first")
            return {"CANCELLED"}

        out_dir = Path(dir_path).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Cannot create directory: {exc}")
            return {"CANCELLED"}

        saved = 0
        for img in bpy.data.images:
            if "df2_mat" not in img:
                continue
            target = out_dir / f"{Path(str(img['df2_mat'])).stem}.png"
            try:
                img.filepath_raw = str(target)
                img.file_format = "PNG"
                img.save()
                saved += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to save %s: %s", target, exc)

        self.report({"INFO"}, f"Saved {saved} texture(s) to {out_dir}")
        log.info("Saved %d textures to %s", saved, out_dir)
        return {"FINISHED"}

    def invoke(self, context, event):
        if not self.directory and context.scene.df2_tex_dir:
            self.directory = context.scene.df2_tex_dir
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class DF2_UL_models(UIList):
    bl_idname = "DF2_UL_models"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        display = getattr(item, "display_name", "") or item.name
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.label(text=display, icon="MESH_CUBE")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon="MESH_CUBE")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        search = (getattr(context.scene, "df2_search", "") or "").lower()
        flags = []
        if search:
            for it in items:
                name = getattr(it, "name", "")
                flags.append(self.bitflag_filter_item if search in name.lower() else 0)
        else:
            flags = [self.bitflag_filter_item] * len(items)
        return flags, []


class DF2_PT_panel(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "DF2 Importer"
    bl_label = "DF2 GOB Importer"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        row = layout.row()
        row.operator(DF2_OT_load_gob.bl_idname, text="Open GOB/GOO", icon="FILE_FOLDER")

        if scene.df2_gob_path:
            layout.label(text=f"Archive: {Path(scene.df2_gob_path).name}")

        layout.template_list(
            DF2_UL_models.bl_idname,
            "",
            scene,
            "df2_models",
            scene,
            "df2_models_index",
            rows=6,
        )

        layout.prop(scene, "df2_tex_dir", text="Texture Output")
        layout.prop(scene, "df2_search", text="Search")
        layout.prop(scene, "df2_geoset_top", text="Only Highest LOD")

        col = layout.column(align=True)
        col.operator(DF2_OT_import_model.bl_idname, icon="IMPORT")
        col.operator(DF2_OT_save_textures.bl_idname, icon="FILE_TICK")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    DF2ModelItem,
    DF2_OT_load_gob,
    DF2_OT_import_model,
    DF2_OT_save_textures,
    DF2_UL_models,
    DF2_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.df2_gob_path = StringProperty(
        name="GOB Path",
        description="Path to the opened GOB/GOO archive",
        default="",
        subtype="FILE_PATH",
    )
    bpy.types.Scene.df2_tex_dir = StringProperty(
        name="Texture Output Directory",
        description="Directory to save extracted textures as PNG (leave empty to keep packed)",
        default="",
        subtype="DIR_PATH",
    )
    bpy.types.Scene.df2_search = StringProperty(
        name="Search",
        description="Filter 3DO names",
        default="",
    )
    bpy.types.Scene.df2_geoset_top = BoolProperty(
        name="Only Highest LOD",
        description="Import only the first (highest detail) geoset to avoid duplicates",
        default=True,
    )
    bpy.types.Scene.df2_models = CollectionProperty(type=DF2ModelItem)
    bpy.types.Scene.df2_models_index = IntProperty(default=0)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.df2_gob_path
    del bpy.types.Scene.df2_tex_dir
    del bpy.types.Scene.df2_search
    del bpy.types.Scene.df2_geoset_top
    del bpy.types.Scene.df2_models
    del bpy.types.Scene.df2_models_index


if __name__ == "__main__":
    register()
