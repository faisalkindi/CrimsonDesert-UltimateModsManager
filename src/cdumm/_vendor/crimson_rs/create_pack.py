
from __future__ import annotations

from pathlib import Path

import crimson_rs
from crimson_rs.enums import Compression, Crypto, Language


def create_pack_group(
    game_dir: str,
    group_name: str,
    files: dict[str, dict[str, bytes]],
    compression: Compression = Compression.LZ4,
    crypto: Crypto = Crypto.NONE,
    encrypt_info: bytes = b"\x00\x00\x00",
    max_chunk_size: int = 500_000_000,
    is_optional: bool = False,
    language: Language = Language.ALL,
) -> None:
    game_path = Path(game_dir)
    group_path = game_path / group_name

    _validate_paths(game_path, group_path)

    builder = crimson_rs.PackGroupBuilder(
        output_dir=str(group_path),
        compression=int(compression),
        crypto=int(crypto),
        encrypt_info=encrypt_info,
        max_chunk_size=max_chunk_size,
    )

    count = 0
    for dir_path, dir_files in files.items():
        for file_name, data in dir_files.items():
            builder.add_file(dir_path, file_name, data)
            count += 1

    if count == 0:
        raise ValueError("No files provided")

    print(f"Packed {count} files into group '{group_name}'")
    pamt_bytes = builder.finish()
    _update_papgt(game_path, group_name, pamt_bytes, is_optional, language)


def create_pack_group_from_folder(
    game_dir: str,
    group_name: str,
    mod_folder: str,
    compression: Compression = Compression.LZ4,
    crypto: Crypto = Crypto.NONE,
    encrypt_info: bytes = b"\x00\x00\x00",
    max_chunk_size: int = 500_000_000,
    is_optional: bool = False,
    language: Language = Language.ALL,
) -> None:
    game_path = Path(game_dir)
    mod_path = Path(mod_folder)
    group_path = game_path / group_name

    if not mod_path.is_dir():
        raise FileNotFoundError(f"Mod folder not found: {mod_folder}")

    _validate_paths(game_path, group_path)

    builder = crimson_rs.PackGroupBuilder(
        output_dir=str(group_path),
        compression=int(compression),
        crypto=int(crypto),
        encrypt_info=encrypt_info,
        max_chunk_size=max_chunk_size,
    )

    count = 0
    for file_path in sorted(mod_path.rglob("*")):
        if not file_path.is_file():
            continue

        rel = file_path.relative_to(mod_path)
        dir_path = str(rel.parent).replace("\\", "/")
        if dir_path == ".":
            dir_path = ""
        file_name = rel.name

        builder.add_file_from_path(dir_path, file_name, str(file_path))
        count += 1

        if count % 100 == 0:
            print(f"  Added {count} files...")

    if count == 0:
        raise ValueError(f"No files found in {mod_folder}")

    print(f"Packed {count} files into group '{group_name}'")
    pamt_bytes = builder.finish()
    _update_papgt(game_path, group_name, pamt_bytes, is_optional, language)


def _validate_paths(game_path: Path, group_path: Path) -> None:
    if not game_path.is_dir():
        raise FileNotFoundError(f"Game directory not found: {game_path}")

    papgt_path = game_path / "meta" / "0.papgt"
    if not papgt_path.exists():
        raise FileNotFoundError(f"PAPGT not found: {papgt_path}")

    if group_path.exists():
        raise FileExistsError(f"Group directory already exists: {group_path}")


def _update_papgt(
    game_path: Path,
    group_name: str,
    pamt_bytes: bytes,
    is_optional: bool,
    language: Language,
) -> None:
    papgt_path = game_path / "meta" / "0.papgt"

    pamt_post_header = pamt_bytes[12:]
    pamt_checksum = crimson_rs.calculate_checksum(pamt_post_header)

    papgt = crimson_rs.parse_papgt_file(str(papgt_path))
    updated_papgt = crimson_rs.add_papgt_entry(
        papgt_data=papgt,
        group_name=group_name,
        pack_meta_checksum=pamt_checksum,
        is_optional=int(is_optional),
        language=int(language),
    )
    crimson_rs.write_papgt_file(updated_papgt, str(papgt_path))
    print(f"Updated {papgt_path} (added entry for '{group_name}')")
    print("Done.")
