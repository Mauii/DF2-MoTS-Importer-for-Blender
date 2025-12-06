#!/usr/bin/env python3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List


# Jedi Knight / DF2 GOB layout (as seen in Res2.gob):
# - Header (20 bytes):
#     0x00: "GOB "
#     0x04: header_size (always 0x14)
#     0x08: version/reserved (0x0C)
#     0x0C: file_count
#     0x10: data_offset (absolute offset where first file's data starts)
# - Directory entries (file_count records) start at header_size (0x14):
#     uint32 size            ; size of this file
#     char name[128]         ; includes subdir, null-terminated, padded
#     uint32 end_offset_abs  ; absolute offset of the end of this file
# Data for all files is laid out sequentially starting at data_offset.


@dataclass
class Entry:
    name: str
    offset: int   # absolute offset in the archive
    size: int


class GOB:
    def __init__(self, archive: str) -> None:
        self.archive_file = archive
        self.archive_data = self._open(archive)
        self.header = Header(self.archive_data)
        self._entries = Entries(self.archive_data, self.header.header)

    def list_entries(self) -> List["Entry"]:
        return self._entries.read_entries()

    def find(self, name: str) -> "Entry | None":
        name_l = name.lower()
        for e in self.list_entries():
            if e.name.lower() == name_l:
                return e
        return None

    def get_data(self, entry: "Entry") -> bytes:
        return self.archive_data[entry.offset : entry.offset + entry.size]

    def extract_to(self, out_dir: Path, overwrite: bool = False) -> List[Path]:
        """
        Extract all entries to out_dir, preserving subfolders from entry names.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: List[Path] = []
        for entry in self.entries.read_entries():
            target = _safe_target(out_dir, entry.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                continue
            data = self.archive_data[entry.offset : entry.offset + entry.size]
            target.write_bytes(data)
            paths.append(target)
        return paths

    @staticmethod
    def _open(archive_file: str) -> bytes:
        with open(archive_file, "rb") as gob:
            return gob.read()


class Header:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.header = self.read_header()

    def read_header(self) -> dict:
        magic, header_size, version, file_count, data_offset = struct.unpack_from("<4sIIII", self.data, 0)
        if magic != b"GOB ":
            raise ValueError("Not a GOB archive")
        return {
            "magic": magic,
            "header_size": header_size,
            "version": version,
            "file_count": file_count,
            "data_offset": data_offset,
        }

    def __repr__(self) -> str:
        h = self.header
        return (
            f"GOB Header(\n"
            f"  Magic={h['magic']},\n"
            f"  Header Size={h['header_size']},\n"
            f"  Version={h['version']},\n"
            f"  File Count={h['file_count']},\n"
            f"  Data Offset={h['data_offset']}\n"
            f")"
        )


class Entries:
    def __init__(self, data: bytes, header: dict) -> None:
        self.data = data
        self.header = header
        self._cache: List[Entry] | None = None

    def read_entries(self) -> List[Entry]:
        if self._cache is not None:
            return self._cache

        h = self.header
        dir_offset = h["header_size"]
        file_count = h["file_count"]
        data_offset = h["data_offset"]
        rec_size = 4 + 128 + 4

        entries: List[Entry] = []
        cursor = data_offset

        for i in range(file_count):
            rec_off = dir_offset + i * rec_size
            if rec_off + rec_size > len(self.data):
                break

            size = struct.unpack_from("<I", self.data, rec_off)[0]
            name_bytes = self.data[rec_off + 4 : rec_off + 4 + 128]
            name = name_bytes.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
            end_offset_abs = struct.unpack_from("<I", self.data, rec_off + 132)[0]

            # Actual file offset is the running cursor; end_offset_abs typically equals cursor + size.
            offset = cursor
            cursor = end_offset_abs if end_offset_abs > cursor else cursor + size

            entries.append(Entry(name=name, offset=offset, size=size))

        self._cache = entries
        return entries


def _safe_target(root: Path, name: str) -> Path:
    """
    Prevent path traversal and preserve archive subdirectories.
    """
    norm = Path(name.replace("\\", "/")).as_posix().lstrip("/\\")
    parts = [p for p in Path(norm).parts if p not in ("", ".", "..")]
    if not parts:
        parts = ["unnamed"]
    return root.joinpath(*parts)


if __name__ == "__main__":
    # Example usage: extract to sibling folder "<archive>_extract"
    archive = GOB(r"C:\Program Files (x86)\Steam\steamapps\common\Star Wars Jedi Knight\Resource\Res2.gob")
    entry_list = archive.entries.read_entries()
    out_dir = Path(archive.archive_file).with_name(f"{Path(archive.archive_file).stem}_extract")
    written = archive.extract_to(out_dir, overwrite=True)
    print(f"Extracted {len(written)} files to {out_dir}")
