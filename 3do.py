"""
Jedi Knight / Dark Forces II 3DO ASCII parser for files like 00crte.3do (3DO 2.1).

Features:
- Parses sections: HEADER, MODELRESOURCE, GEOMETRYDEF, HIERARCHYDEF
- Reads:
  * materials
  * vertices
  * texture vertices
  * vertex normals
  * faces (with vertex & tex-vertex indices)
  * face normals
  * simple hierarchy nodes (name + mesh index; rest kept raw)
- Provides simple CLI summary.

Usage (CLI):
    python jk3do.py 00crte.3do

Then extend / integrate into your GOB viewer or Blender importer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Vertex:
    x: float
    y: float
    z: float
    intensity: float


@dataclass
class TexVertex:
    u: float
    v: float


@dataclass
class Normal:
    x: float
    y: float
    z: float


@dataclass
class Face:
    index: int
    material: int
    type_flags: int
    geomode: int
    lightmode: int
    texmode: int
    extralight: float
    vertex_indices: List[int]
    texvertex_indices: List[int]


@dataclass
class Mesh:
    index: int
    name: str
    radius: float
    geometry_mode: int
    lighting_mode: int
    texture_mode: int

    vertices: List[Vertex] = field(default_factory=list)
    texvertices: List[TexVertex] = field(default_factory=list)
    vertex_normals: List[Normal] = field(default_factory=list)
    faces: List[Face] = field(default_factory=list)
    face_normals: List[Normal] = field(default_factory=list)


@dataclass
class GeoSet:
    index: int
    meshes: List[Mesh] = field(default_factory=list)


@dataclass
class Material:
    index: int
    name: str


@dataclass
class HierarchyNode:
    index: int
    raw_line: str
    flags: Optional[int] = None
    type_flags: Optional[int] = None
    mesh_index: Optional[int] = None
    parent_index: Optional[int] = None
    child_index: Optional[int] = None
    sibling_index: Optional[int] = None
    num_children: Optional[int] = None
    position: Optional[tuple[float, float, float]] = None
    rotation: Optional[tuple[float, float, float]] = None
    pivot: Optional[tuple[float, float, float]] = None
    name: Optional[str] = None


@dataclass
class ThreeDO:
    path: Path
    version: str
    materials: List[Material]
    geosets: List[GeoSet]
    hierarchy_nodes: List[HierarchyNode]
    object_radius: Optional[float] = None
    insert_offset: Optional[tuple[float, float, float]] = None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class ThreeDOParser:
    def __init__(self, text: str):
        # strip comments (# ..) but keep original lines for some parsing
        self.raw_lines = text.splitlines()
        self.lines = [line.split("#", 1)[0].rstrip() for line in self.raw_lines]
        self.pos = 0
        self.n = len(self.lines)

    # ------------- helpers -------------

    def _next_line(self) -> Optional[str]:
        while self.pos < self.n:
            line = self.lines[self.pos]
            self.pos += 1
            if line.strip():
                return line
        return None

    def _peek_line(self) -> Optional[str]:
        p = self.pos
        while p < self.n:
            line = self.lines[p]
            if line.strip():
                return line
            p += 1
        return None

    def _expect_prefix(self, prefix: str) -> str:
        line = self._next_line()
        if line is None or not line.strip().startswith(prefix):
            raise ValueError(f"Expected line starting with {prefix!r}, got {line!r}")
        return line

    # ------------- top level -------------

    def parse(self, path: Path) -> ThreeDO:
        version = ""
        materials: List[Material] = []
        geosets: List[GeoSet] = []
        hierarchy_nodes: List[HierarchyNode] = []
        object_radius: Optional[float] = None
        insert_offset: Optional[tuple[float, float, float]] = None

        while True:
            line = self._next_line()
            if line is None:
                break
            s = line.strip()
            if not s:
                continue

            if s.startswith("SECTION:"):
                section = s[len("SECTION:"):].strip().upper()
                if section == "HEADER":
                    version = self._parse_header()
                elif section == "MODELRESOURCE":
                    materials = self._parse_modelresource()
                elif section == "GEOMETRYDEF":
                    radius, ins_off, geosets = self._parse_geometrydef()
                    object_radius = radius
                    insert_offset = ins_off
                elif section == "HIERARCHYDEF":
                    hierarchy_nodes = self._parse_hierarchydef()
                else:
                    # unknown section: skip
                    pass

        return ThreeDO(
            path=path,
            version=version,
            materials=materials,
            geosets=geosets,
            hierarchy_nodes=hierarchy_nodes,
            object_radius=object_radius,
            insert_offset=insert_offset,
        )

    # ------------- sections -------------

    def _parse_header(self) -> str:
        # First non-empty line is "3DO 2.1"
        while True:
            line = self._next_line()
            if line is None:
                raise ValueError("Unexpected EOF in HEADER")
            s = line.strip()
            if s:
                # example: "3DO 2.1"
                parts = s.split()
                if parts[0] != "3DO":
                    raise ValueError(f"Unexpected HEADER line: {s!r}")
                version = parts[1] if len(parts) > 1 else ""
                return version

    def _parse_modelresource(self) -> List[Material]:
        materials: List[Material] = []
        # Expect "MATERIALS N"
        while True:
            line = self._next_line()
            if line is None:
                break
            s = line.strip()
            if not s:
                continue
            if s.startswith("MATERIALS"):
                parts = s.split()
                count = int(parts[1])
                for _ in range(count):
                    line = self._next_line()
                    if line is None:
                        raise ValueError("Unexpected EOF in MATERIALS list")
                    # example: "         0:   00crtesd.mat"
                    no_comment = line.split("#", 1)[0].strip()
                    if not no_comment:
                        continue
                    idx_str, rest = no_comment.split(":", 1)
                    index = int(idx_str)
                    name = rest.strip()
                    materials.append(Material(index=index, name=name))
                break
            # else skip until we see MATERIALS
        return materials

    def _parse_geometrydef(self) -> tuple[Optional[float], Optional[tuple[float, float, float]], List[GeoSet]]:
        object_radius: Optional[float] = None
        insert_offset: Optional[tuple[float, float, float]] = None
        geosets: List[GeoSet] = []

        # We expect RADIUS, INSERT OFFSET, GEOSETS etc, but we parse flexibly.
        while True:
            line = self._next_line()
            if line is None:
                break
            s = line.strip()
            if not s:
                continue
            if s.startswith("RADIUS"):
                # "RADIUS   0.830224"
                parts = s.split()
                if len(parts) >= 2:
                    object_radius = float(parts[1])
            elif s.startswith("INSERT OFFSET"):
                # "INSERT OFFSET   0.000  0.000  0.000"
                parts = s.split()
                if len(parts) >= 4:
                    insert_offset = (float(parts[2]), float(parts[3]), float(parts[4]))
            elif s.startswith("GEOSETS"):
                # "GEOSETS 1"
                parts = s.split()
                geoset_count = int(parts[1])
                for _ in range(geoset_count):
                    geoset = self._parse_geoset()
                    geosets.append(geoset)
                break
            elif s.startswith("SECTION:"):
                # next section; step back one line for outer loop
                self.pos -= 1
                break

        return object_radius, insert_offset, geosets

    def _parse_geoset(self) -> GeoSet:
        # Expect "GEOSET idx"
        while True:
            line = self._next_line()
            if line is None:
                raise ValueError("Unexpected EOF in GEOSET")
            s = line.strip()
            if not s:
                continue
            if s.startswith("GEOSET"):
                parts = s.split()
                geoset_index = int(parts[1])
                break

        # Expect "MESHES N"
        while True:
            line = self._next_line()
            if line is None:
                raise ValueError("Unexpected EOF in GEOSET (MESHES)")
            s = line.strip()
            if not s:
                continue
            if s.startswith("MESHES"):
                parts = s.split()
                mesh_count = int(parts[1])
                break

        meshes: List[Mesh] = []
        for _ in range(mesh_count):
            mesh = self._parse_mesh()
            meshes.append(mesh)

        return GeoSet(index=geoset_index, meshes=meshes)

    def _parse_mesh(self) -> Mesh:
        # We expect:
        #   MESH <idx>
        #   NAME <name>
        #   RADIUS <float>
        #   GEOMETRYMODE <int>
        #   LIGHTINGMODE <int>
        #   TEXTUREMODE <int>
        mesh_index = -1
        mesh_name = ""
        radius = 0.0
        geomode = 4
        lightmode = 3
        texmode = 1

        # read up to VERTICES
        while True:
            line = self._next_line()
            if line is None:
                raise ValueError("Unexpected EOF in MESH")
            s = line.strip()
            if not s:
                continue
            if s.startswith("MESH"):
                mesh_index = int(s.split()[1])
            elif s.startswith("NAME"):
                # NAME <identifier>
                mesh_name = s.split(maxsplit=1)[1]
            elif s.startswith("RADIUS"):
                radius = float(s.split()[1])
            elif s.startswith("GEOMETRYMODE"):
                geomode = int(s.split()[1])
            elif s.startswith("LIGHTINGMODE"):
                lightmode = int(s.split()[1])
            elif s.startswith("TEXTUREMODE"):
                texmode = int(s.split()[1])
            elif s.startswith("VERTICES"):
                # we've reached geometry arrays, back up one line
                self.pos -= 1
                break

        mesh = Mesh(
            index=mesh_index,
            name=mesh_name,
            radius=radius,
            geometry_mode=geomode,
            lighting_mode=lightmode,
            texture_mode=texmode,
        )

        # VERTICES
        mesh.vertices = self._parse_vertices()
        # TEXTURE VERTICES
        mesh.texvertices = self._parse_texvertices()
        # VERTEX NORMALS
        mesh.vertex_normals = self._parse_vertex_normals(len(mesh.vertices))
        # FACES
        mesh.faces = self._parse_faces()
        # FACE NORMALS
        mesh.face_normals = self._parse_face_normals(len(mesh.faces))

        return mesh

    def _parse_vertices(self) -> List[Vertex]:
        # Expect "VERTICES N"
        line = self._expect_prefix("VERTICES")
        parts = line.strip().split()
        count = int(parts[1])

        verts: List[Vertex] = []
        read = 0
        while read < count:
            line = self._next_line()
            if line is None:
                raise ValueError("Unexpected EOF in VERTICES")
            s = line.strip()
            if not s:
                continue
            if ":" not in s:
                continue
            idx_str, rest = s.split(":", 1)
            # rest: "   0.597179  -0.400029  -0.299695   0.000000"
            vals = rest.split()
            if len(vals) < 4:
                continue
            x, y, z, i = map(float, vals[:4])
            verts.append(Vertex(x=x, y=y, z=z, intensity=i))
            read += 1
        return verts

    def _parse_texvertices(self) -> List[TexVertex]:
        # Expect "TEXTURE VERTICES N"
        line = self._expect_prefix("TEXTURE VERTICES")
        parts = line.strip().split()
        count = int(parts[2])  # TEXTURE VERTICES <N>

        tverts: List[TexVertex] = []
        read = 0
        while read < count:
            line = self._next_line()
            if line is None:
                raise ValueError("Unexpected EOF in TEXTURE VERTICES")
            s = line.strip()
            if not s or ":" not in s:
                continue
            _, rest = s.split(":", 1)
            vals = rest.split()
            if len(vals) < 2:
                continue
            u, v = map(float, vals[:2])
            tverts.append(TexVertex(u=u, v=v))
            read += 1
        return tverts

    def _parse_vertex_normals(self, expected_count: int) -> List[Normal]:
        # Look for "VERTEX NORMALS"
        while True:
            line = self._next_line()
            if line is None:
                return []
            s = line.strip()
            if not s:
                continue
            if s.startswith("VERTEX NORMALS"):
                break
            if s.startswith("FACES"):
                # no normals
                self.pos -= 1
                return []

        normals: List[Normal] = []
        read = 0
        while read < expected_count:
            line = self._next_line()
            if line is None:
                break
            s = line.strip()
            if not s or ":" not in s:
                continue
            _, rest = s.split(":", 1)
            vals = rest.split()
            if len(vals) < 3:
                continue
            x, y, z = map(float, vals[:3])
            normals.append(Normal(x=x, y=y, z=z))
            read += 1
        return normals

    def _parse_faces(self) -> List[Face]:
        # Expect "FACES N"
        while True:
            line = self._next_line()
            if line is None:
                return []
            s = line.strip()
            if not s:
                continue
            if s.startswith("FACES"):
                parts = s.split()
                count = int(parts[1])
                break
        faces: List[Face] = []
        read = 0
        while read < count:
            line = self._next_line()
            if line is None:
                raise ValueError("Unexpected EOF in FACES")
            s = line.strip()
            if not s or ":" not in s or s.startswith("#"):
                continue

            idx_str, rest = s.split(":", 1)
            face_index = int(idx_str)

            # Normalize commas: "0,  0" -> "0 , 0"
            rest_norm = rest.replace(",", " , ")
            tokens = rest_norm.split()

            # layout:
            # material, type, geo, light, tex, extralight, verts, [v, ',', t, v, ',', t, ...]
            if len(tokens) < 7:
                continue

            material = int(tokens[0])
            type_str = tokens[1]
            try:
                type_flags = int(type_str, 0)
            except ValueError:
                type_flags = 0
            geomode = int(tokens[2])
            lightmode = int(tokens[3])
            texmode = int(tokens[4])
            extralight = float(tokens[5])
            vert_count = int(tokens[6])

            vi: List[int] = []
            ti: List[int] = []
            j = 7
            while j < len(tokens):
                # vertex index
                try:
                    v_idx = int(tokens[j])
                except ValueError:
                    break
                j += 1
                # optional comma
                if j < len(tokens) and tokens[j] == ",":
                    j += 1
                if j >= len(tokens):
                    break
                # texvertex index
                try:
                    t_idx = int(tokens[j])
                except ValueError:
                    break
                j += 1
                vi.append(v_idx)
                ti.append(t_idx)

            faces.append(
                Face(
                    index=face_index,
                    material=material,
                    type_flags=type_flags,
                    geomode=geomode,
                    lightmode=lightmode,
                    texmode=texmode,
                    extralight=extralight,
                    vertex_indices=vi,
                    texvertex_indices=ti,
                )
            )
            read += 1
        return faces

    def _parse_face_normals(self, expected_count: int) -> List[Normal]:
        # Look for "FACE NORMALS"
        while True:
            line = self._next_line()
            if line is None:
                return []
            s = line.strip()
            if not s:
                continue
            if s.startswith("FACE NORMALS"):
                break
            if s.startswith("SECTION:"):
                self.pos -= 1
                return []

        normals: List[Normal] = []
        read = 0
        while read < expected_count:
            line = self._next_line()
            if line is None:
                break
            s = line.strip()
            if not s or ":" not in s:
                continue
            _, rest = s.split(":", 1)
            vals = rest.split()
            if len(vals) < 3:
                continue
            x, y, z = map(float, vals[:3])
            normals.append(Normal(x=x, y=y, z=z))
            read += 1
        return normals

    def _parse_hierarchydef(self) -> List[HierarchyNode]:
        nodes: List[HierarchyNode] = []

        # find "HIERARCHY NODES N"
        while True:
            line = self._next_line()
            if line is None:
                return nodes
            s = line.strip()
            if not s:
                continue
            if s.startswith("HIERARCHY NODES"):
                break

        # now parse node lines (until next SECTION or EOF)
        while True:
            if self.pos >= self.n:
                break
            raw_line = self.raw_lines[self.pos]
            line = self.lines[self.pos]
            self.pos += 1

            s = line.strip()
            if not s:
                continue
            if s.startswith("SECTION:"):
                self.pos -= 1
                break
            if s.startswith("#"):
                continue
            if ":" not in s:
                continue

            idx_str, rest = s.split(":", 1)
            idx = int(idx_str)
            tokens = rest.split()

            flags = type_flags = mesh_index = parent_idx = child_idx = sibling_idx = num_children = None
            pos = rot = pivot = None
            name = None

            try:
                flags = int(tokens[0], 0)
                type_flags = int(tokens[1], 0)
                mesh_index = int(tokens[2])
                parent_idx = int(tokens[3])
                child_idx = int(tokens[4])
                sibling_idx = int(tokens[5])
                num_children = int(tokens[6])
                x, y, z = map(float, tokens[7:10])
                pitch, yaw, roll = map(float, tokens[10:13])
                pivotx, pivoty, pivotz = map(float, tokens[13:16])
                pos = (x, y, z)
                rot = (pitch, yaw, roll)
                pivot = (pivotx, pivoty, pivotz)
                if len(tokens) > 16:
                    name = " ".join(tokens[16:])
            except Exception:
                # fall back to last token as name if parsing failed
                if tokens:
                    name = tokens[-1]

            nodes.append(
                HierarchyNode(
                    index=idx,
                    raw_line=raw_line,
                    flags=flags,
                    type_flags=type_flags,
                    mesh_index=mesh_index,
                    parent_index=parent_idx,
                    child_index=child_idx,
                    sibling_index=sibling_idx,
                    num_children=num_children,
                    position=pos,
                    rotation=rot,
                    pivot=pivot,
                    name=name,
                )
            )

        return nodes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_3do(path: str | Path) -> ThreeDO:
    p = Path(path)
    text = p.read_text(encoding="ascii", errors="ignore")
    parser = ThreeDOParser(text)
    return parser.parse(p)


def _main():
    import argparse

    ap = argparse.ArgumentParser(description="Parse Jedi Knight / DF2 3DO ASCII file")
    ap.add_argument("file", help="Path to .3do file")
    args = ap.parse_args()

    three = load_3do(args.file)

    print(f"3DO file: {three.path}")
    print(f"Version : {three.version}")
    if three.object_radius is not None:
        print(f"Object radius: {three.object_radius}")
    if three.insert_offset is not None:
        print(f"Insert offset: {three.insert_offset}")

    print()
    print(f"Materials ({len(three.materials)}):")
    for m in three.materials:
        print(f"  {m.index}: {m.name}")

    print()
    print(f"GeoSets ({len(three.geosets)}):")
    for gs in three.geosets:
        print(f"  GeoSet {gs.index}: {len(gs.meshes)} mesh(es)")
        for mesh in gs.meshes:
            print(f"    Mesh {mesh.index} '{mesh.name}':")
            print(f"      radius        : {mesh.radius}")
            print(f"      geometry mode : {mesh.geometry_mode}")
            print(f"      lighting mode : {mesh.lighting_mode}")
            print(f"      texture mode  : {mesh.texture_mode}")
            print(f"      vertices      : {len(mesh.vertices)}")
            print(f"      texvertices   : {len(mesh.texvertices)}")
            print(f"      faces         : {len(mesh.faces)}")

    print()
    print(f"Hierarchy nodes ({len(three.hierarchy_nodes)}):")
    for node in three.hierarchy_nodes:
        print(
            f"  {node.index}: name={node.name!r} "
            f"mesh={node.mesh_index} flags={node.flags} type={node.type_flags}"
        )


if __name__ == "__main__":
    _main()
