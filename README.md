# DF2 / MotS 3DO Importer for Blender

Version: 0.2.0  
Blender: 3.0+

Import Jedi Knight: Dark Forces II / Mysteries of the Sith 3DO models directly from GOB/GOO archives. Materials are decoded from MAT files, LOD GeoSets are handled, and hierarchy transforms (pivots, rotations, insert offsets) are applied to place parts correctly in Blender.

## Features
- Reads 3DO 2.1 files from a selected GOB/GOO.
- Builds meshes, UVs, and applies MAT textures (8-bit with CMP palette or 16-bit); Blender handles normals itself.
- Supports multiple GeoSets (LOD); option to import only the highest LOD.
- Preserves hierarchy transforms (positions, rotations, pivots, insert offset) to correctly place limbs and parts.
- Texture saver: export imported MATs to PNG.

## Installation
1. Download or clone this repo into your Blender add-ons directory, e.g.  
   `C:\Users\<you>\AppData\Roaming\Blender Foundation\Blender\<version>\scripts\addons\DF2-Importer-for-Blender`
2. In Blender: **Edit → Preferences → Add-ons → Install...** and pick the folder (or zip) containing this add-on.
3. Enable **DF2 GOB 3DO Importer**.

## Usage
1. Open **View3D → Sidebar → DF2 Importer**.
2. Click **Open GOB/GOO**, pick your archive.
3. Select a 3DO from the list (filter with **Search** if needed).
4. Set options:
   - **Only Highest LOD**: import GeoSet 0 only. (recommended to leave this on)
   - **Texture Output**: optional directory to save decoded PNGs (otherwise textures stay packed).
5. Click **Import**. Meshes appear under a parent object named after the 3DO.
6. To export textures later, use **Save DF2 Textures**.

## Notes & Limits
- Some 3DOs ship with empty meshes (e.g. `k_rhand` in all `kyx.3do` files), so missing parts can be created with the mirror modifier if need be.
- UVs use the MAT’s frame dimensions.
- Only 3DO geometry/materials are imported; keyframes/animations are not included.

## Building / Versioning
- Current add-on version is tracked in `__init__.py` (`bl_info["version"]`).
- Edit files and commit as usual; no extra build steps are required.
