from pathlib import Path
import struct
from PIL import Image

# ------------------------------------------------------
# Load CMP (JK Colormap) – first 768 bytes = palette
# ------------------------------------------------------
def load_cmp_palette(cmp_path: str) -> list[int]:
    data = Path(cmp_path).read_bytes()

    if len(data) < 768:
        raise ValueError(f"CMP too small (expected at least 768 bytes): {cmp_path}")

    pal = list(data[:768])   # 256 * 3 RGB bytes
    return pal


# ------------------------------------------------------
# Parse MAT header (MAT2)
# ------------------------------------------------------
def parse_mat_header(data: bytes):
    if data[:5] != b"MAT 2":
        raise ValueError("Not a MAT 2 file. (Unsupported old Dark Forces MAT?)")

    mat_type = struct.unpack_from("<I", data, 8)[0]
    record_count = struct.unpack_from("<I", data, 12)[0]
    bit_depth = struct.unpack_from("<I", data, 24)[0]

    return mat_type, record_count, bit_depth


# ------------------------------------------------------
# Extract first image frame from MAT
# Works for both 8-bit and 16-bit
# ------------------------------------------------------
def extract_mat_pixels(data: bytes, bit_depth: int):
    # Skip MAT header: 76 bytes + per-record table (40 bytes * record_count)
    record_count = struct.unpack_from("<I", data, 12)[0]
    pixel_offset = 76 + record_count * 40

    # Read image block header
    width = struct.unpack_from("<I", data, pixel_offset)[0]
    height = struct.unpack_from("<I", data, pixel_offset + 4)[0]

    pixel_data_offset = pixel_offset + 24  # skip width,height,trans,2 unknowns,num_mipmaps

    if bit_depth == 8:
        size = width * height
        pixels = data[pixel_data_offset:pixel_data_offset + size]
        return width, height, pixels

    elif bit_depth == 16:
        size = width * height * 2
        pixels = data[pixel_data_offset:pixel_data_offset + size]
        return width, height, pixels

    else:
        raise ValueError(f"Unsupported bit depth: {bit_depth}")


# ------------------------------------------------------
# Build a PNG from 8-bit MAT
# ------------------------------------------------------
def mat8_to_png(mat_path: str, cmp_path: str, out_path: str):
    data = Path(mat_path).read_bytes()

    mat_type, record_count, bit_depth = parse_mat_header(data)
    if bit_depth != 8:
        raise ValueError("This MAT is not 8-bit. Use mat16_to_png instead.")

    width, height, pixels = extract_mat_pixels(data, bit_depth)
    palette = load_cmp_palette(cmp_path)

    # Create indexed image
    img = Image.frombytes("P", (width, height), pixels)
    img.putpalette(palette)

    # Convert to RGBA (optional but preferred)
    img = img.convert("RGBA")
    img.save(out_path)

    print(f"[OK] Saved 8-bit MAT → {out_path}")


# ------------------------------------------------------
# Build a PNG from 16-bit MAT (RGB555 / 565)
# Uses JK’s bit layout in header
# ------------------------------------------------------
def mat16_to_png(mat_path: str, out_path: str):
    data = Path(mat_path).read_bytes()

    mat_type, record_count, bit_depth = parse_mat_header(data)
    if bit_depth != 16:
        raise ValueError("This MAT is not 16-bit. Use mat8_to_png instead.")

    width, height, pixels = extract_mat_pixels(data, bit_depth)

    # MAT header fields for bit shifts
    blue_bits  = struct.unpack_from("<I", data, 28)[0]
    green_bits = struct.unpack_from("<I", data, 32)[0]
    red_bits   = struct.unpack_from("<I", data, 36)[0]

    blue_shift  = struct.unpack_from("<I", data, 48)[0]
    green_shift = struct.unpack_from("<I", data, 44)[0]
    red_shift   = struct.unpack_from("<I", data, 40)[0]

    rgb = []
    for i in range(0, len(pixels), 2):
        val = struct.unpack_from("<H", pixels, i)[0]

        r = ((val >> red_shift)   & ((1 << red_bits)   - 1))
        g = ((val >> green_shift) & ((1 << green_bits) - 1))
        b = ((val >> blue_shift)  & ((1 << blue_bits)  - 1))

        # Normalize to 0–255
        r = int(r * 255 / ((1 << red_bits) - 1))
        g = int(g * 255 / ((1 << green_bits) - 1))
        b = int(b * 255 / ((1 << blue_bits) - 1))

        rgb.append((r, g, b))

    img = Image.new("RGB", (width, height))
    img.putdata(rgb)
    img.save(out_path)

    print(f"[OK] Saved 16-bit MAT → {out_path}")


# ------------------------------------------------------
# AUTO wrapper
# ------------------------------------------------------
def mat_to_png(mat_path: str, cmp_path: str | None, out_path: str):
    data = Path(mat_path).read_bytes()
    mat_type, record_count, bit_depth = parse_mat_header(data)

    if bit_depth == 8:
        if cmp_path is None:
            raise ValueError("8-bit MAT requires CMP palette.")
        return mat8_to_png(mat_path, cmp_path, out_path)

    elif bit_depth == 16:
        return mat16_to_png(mat_path, out_path)

    else:
        raise ValueError("Unknown MAT format.")
