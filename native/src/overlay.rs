//! Overlay PAZ + PAMT builder for Crimson Desert.
//!
//! Builds a fresh overlay PAZ + PAMT from a list of [`OverlayInput`] entries.
//! The overlay directory replaces modifying original game files in-place.
//! The game loads entries from the overlay directory first, leaving vanilla
//! files untouched.
//!
//! Ported from `cdumm/archive/overlay_builder.py`.  Matches JSON Mod Manager's
//! BuildMultiPamt format exactly.

use std::collections::BTreeMap;

use crate::dds;
use crate::hashlittle::{hashlittle, HASH_SEED};
use crate::lz4_util;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAZ_ALIGNMENT: usize = 16;
const PAMT_CONSTANT: u32 = 0x610E0232;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/// A single entry to include in the overlay PAZ.
pub struct OverlayInput {
    /// Folder path (e.g. `"gamedata/binary__/client/bin"`).
    pub dir_path: String,
    /// File basename (e.g. `"inventory.pabgb"`).
    pub filename: String,
    /// Decompressed content.
    pub content: Vec<u8>,
    /// Compression type: 0 = none, 1 = DDS split, 2 = LZ4.
    pub compression_type: u32,
}

/// Result of building an overlay.
pub struct OverlayResult {
    pub paz_bytes: Vec<u8>,
    pub pamt_bytes: Vec<u8>,
    pub entry_count: u32,
}

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

/// Internal tracking for built entries.
struct OverlayEntry {
    dir_path: String,
    filename: String,
    paz_offset: u32,
    comp_size: u32,
    decomp_size: u32,
    flags: u16,
}

// ---------------------------------------------------------------------------
// Little-endian write helpers
// ---------------------------------------------------------------------------

fn push_u32(buf: &mut Vec<u8>, val: u32) {
    buf.extend_from_slice(&val.to_le_bytes());
}

// ---------------------------------------------------------------------------
// PAMT builder
// ---------------------------------------------------------------------------

/// Build a PAMT file matching JSON MM's BuildMultiPamt format.
///
/// Layout:
/// ```text
/// [0:4]   outer_hash (hashlittle(pamt[12:], HASH_SEED)) - placeholder
/// [4:8]   paz_count = 1
/// [8:12]  constant = 0x610E0232
/// [12:16] zero = 0
/// [16:20] PAZ CRC (placeholder, filled by caller)
/// [20:24] PAZ data length
/// folder_section_len(4) + folder_bytes
/// node_section_len(4) + node_bytes
/// folder_count(4) + folder_records (16 bytes each)
/// file_count(4) + file_records (20 bytes each)
/// ```
fn build_pamt(entries: &[OverlayEntry], paz_data_len: usize) -> Vec<u8> {
    // Collect unique directories in sorted order.
    let mut unique_dirs_set = std::collections::BTreeSet::new();
    for e in entries {
        unique_dirs_set.insert(e.dir_path.clone());
    }
    let unique_dirs: Vec<String> = unique_dirs_set.into_iter().collect();

    // ── Folder section (directory tree) ──
    let mut folder_bytes: Vec<u8> = Vec::new();
    let mut folder_offsets: BTreeMap<String, u32> = BTreeMap::new();

    for dir_path in &unique_dirs {
        let parts: Vec<&str> = if dir_path.is_empty() {
            vec![""]
        } else {
            dir_path.split('/').collect()
        };

        for depth in 0..parts.len() {
            let key: String = parts[..=depth].join("/");
            if folder_offsets.contains_key(&key) {
                continue;
            }
            let offset = folder_bytes.len() as u32;
            folder_offsets.insert(key.clone(), offset);

            let (parent, name) = if depth == 0 {
                (0xFFFF_FFFFu32, parts[0].to_string())
            } else {
                let parent_key: String = parts[..depth].join("/");
                let parent = folder_offsets[&parent_key];
                let name = format!("/{}", parts[depth]);
                (parent, name)
            };

            let name_bytes = name.as_bytes();
            push_u32(&mut folder_bytes, parent);
            folder_bytes.push(name_bytes.len() as u8);
            folder_bytes.extend_from_slice(name_bytes);
        }
    }

    // ── Group and sort entries by directory ──
    // Use BTreeMap for deterministic order.  Within each directory, sort by
    // filename.  Track each entry's original index so we can look up the
    // corresponding OverlayEntry later.
    let mut dir_entries: BTreeMap<&str, Vec<(usize, &OverlayEntry)>> = BTreeMap::new();
    for (i, e) in entries.iter().enumerate() {
        dir_entries
            .entry(e.dir_path.as_str())
            .or_default()
            .push((i, e));
    }
    for group in dir_entries.values_mut() {
        group.sort_by(|a, b| a.1.filename.cmp(&b.1.filename));
    }

    // ── Node section (filenames — flat list) ──
    let mut node_bytes: Vec<u8> = Vec::new();
    // Maps original entry index -> node offset within node section.
    let mut node_offsets: BTreeMap<usize, u32> = BTreeMap::new();

    for dir_path in &unique_dirs {
        if let Some(group) = dir_entries.get(dir_path.as_str()) {
            for &(idx, entry) in group {
                let node_offset = node_bytes.len() as u32;
                node_offsets.insert(idx, node_offset);

                let name_bytes = entry.filename.as_bytes();
                push_u32(&mut node_bytes, 0xFFFF_FFFF); // parent = none
                node_bytes.push(name_bytes.len() as u8);
                node_bytes.extend_from_slice(name_bytes);
            }
        }
    }

    // ── Folder records (16 bytes each) ──
    let mut folder_records: Vec<u8> = Vec::new();
    let mut file_index: u32 = 0;

    for dir_path in &unique_dirs {
        let count = dir_entries
            .get(dir_path.as_str())
            .map_or(0, |g| g.len()) as u32;
        let path_hash = hashlittle(dir_path.as_bytes(), HASH_SEED);
        let folder_ref = folder_offsets.get(dir_path.as_str()).copied().unwrap_or(0);

        push_u32(&mut folder_records, path_hash);
        push_u32(&mut folder_records, folder_ref);
        push_u32(&mut folder_records, file_index);
        push_u32(&mut folder_records, count);

        file_index += count;
    }

    // ── File records (20 bytes each) ──
    let mut file_records: Vec<u8> = Vec::new();
    for dir_path in &unique_dirs {
        if let Some(group) = dir_entries.get(dir_path.as_str()) {
            for &(idx, entry) in group {
                let node_ref = node_offsets[&idx];
                push_u32(&mut file_records, node_ref);
                push_u32(&mut file_records, entry.paz_offset);
                push_u32(&mut file_records, entry.comp_size);
                push_u32(&mut file_records, entry.decomp_size);
                // Flags as u32: low byte = paz_index (0 for overlay),
                // bits 16-19 = compression_type.  Must match parser's
                // read_u32 at paz.rs:231.
                let flags_u32 = (entry.flags as u32) << 16;
                push_u32(&mut file_records, flags_u32);
            }
        }
    }

    // ── Assemble PAMT ──
    // Body starts at offset 4 (after outer_hash placeholder).
    let mut body: Vec<u8> = Vec::new();
    push_u32(&mut body, 1);                         // paz_count
    push_u32(&mut body, PAMT_CONSTANT);             // constant
    push_u32(&mut body, 0);                         // zero
    push_u32(&mut body, 0);                         // PAZ CRC placeholder
    push_u32(&mut body, paz_data_len as u32);       // PAZ size

    push_u32(&mut body, folder_bytes.len() as u32);
    body.extend_from_slice(&folder_bytes);

    push_u32(&mut body, node_bytes.len() as u32);
    body.extend_from_slice(&node_bytes);

    push_u32(&mut body, unique_dirs.len() as u32);
    body.extend_from_slice(&folder_records);

    push_u32(&mut body, file_index);                // file_count
    body.extend_from_slice(&file_records);

    // Prepend outer hash placeholder (4 zero bytes).
    let mut pamt = Vec::with_capacity(4 + body.len());
    push_u32(&mut pamt, 0); // outer_hash placeholder
    pamt.extend_from_slice(&body);

    pamt
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

/// Build overlay PAZ + PAMT from a list of entries.
///
/// For each input:
/// - compression_type 0: store raw, flags = 0
/// - compression_type 1: DDS split compress, flags = 1
/// - compression_type 2: LZ4 block compress, flags = 2
///
/// After building both buffers, the PAZ CRC is patched into PAMT offset 16,
/// and the outer hash is written at PAMT offset 0.
pub fn build_overlay(inputs: Vec<OverlayInput>) -> OverlayResult {
    let mut paz_buf: Vec<u8> = Vec::new();
    let mut overlay_entries: Vec<OverlayEntry> = Vec::new();

    for input in &inputs {
        let paz_offset = paz_buf.len() as u32;

        let (payload, comp_size, decomp_size, flags): (Vec<u8>, u32, u32, u16) =
            match input.compression_type {
                1 => {
                    // DDS split compression
                    let (payload, cs, ds) = dds::compress_dds(&input.content);
                    (payload, cs, ds, 1)
                }
                2 => {
                    // LZ4 block compression
                    let compressed = lz4_util::compress(&input.content);
                    let comp_size = compressed.len() as u32;
                    let decomp_size = input.content.len() as u32;
                    (compressed, comp_size, decomp_size, 2)
                }
                _ => {
                    // No compression (type 0 or unknown)
                    let size = input.content.len() as u32;
                    (input.content.clone(), size, size, 0)
                }
            };

        paz_buf.extend_from_slice(&payload);

        // Pad to 16-byte alignment.
        let remainder = paz_buf.len() % PAZ_ALIGNMENT;
        if remainder != 0 {
            let pad = PAZ_ALIGNMENT - remainder;
            paz_buf.resize(paz_buf.len() + pad, 0);
        }

        overlay_entries.push(OverlayEntry {
            dir_path: input.dir_path.clone(),
            filename: input.filename.clone(),
            paz_offset,
            comp_size,
            decomp_size,
            flags,
        });
    }

    let entry_count = overlay_entries.len() as u32;
    let paz_bytes = paz_buf;

    // Build PAMT.
    let mut pamt_bytes = build_pamt(&overlay_entries, paz_bytes.len());

    // Patch PAZ CRC into PAMT at offset 16.
    let paz_crc = hashlittle(&paz_bytes, HASH_SEED);
    pamt_bytes[16..20].copy_from_slice(&paz_crc.to_le_bytes());

    // Recompute outer hash over pamt[12:] and write at offset 0.
    let outer_hash = hashlittle(&pamt_bytes[12..], HASH_SEED);
    pamt_bytes[0..4].copy_from_slice(&outer_hash.to_le_bytes());

    OverlayResult {
        paz_bytes,
        pamt_bytes,
        entry_count,
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // ── Helper: read a u32 LE from a byte slice ──

    fn read_u32(data: &[u8], offset: usize) -> u32 {
        u32::from_le_bytes([
            data[offset],
            data[offset + 1],
            data[offset + 2],
            data[offset + 3],
        ])
    }

    // ── Empty overlay ──

    #[test]
    fn test_empty_overlay() {
        let result = build_overlay(vec![]);
        assert_eq!(result.entry_count, 0);
        assert!(result.paz_bytes.is_empty());
        // PAMT should still have a valid header.
        assert!(result.pamt_bytes.len() >= 24);
        // paz_count = 1
        assert_eq!(read_u32(&result.pamt_bytes, 4), 1);
        // constant
        assert_eq!(read_u32(&result.pamt_bytes, 8), PAMT_CONSTANT);
        // zero
        assert_eq!(read_u32(&result.pamt_bytes, 12), 0);
    }

    // ── Single uncompressed entry ──

    #[test]
    fn test_single_uncompressed_entry() {
        let content = b"hello world".to_vec();
        let content_len = content.len();

        let result = build_overlay(vec![OverlayInput {
            dir_path: "gamedata".into(),
            filename: "test.bin".into(),
            content,
            compression_type: 0,
        }]);

        assert_eq!(result.entry_count, 1);
        // PAZ should contain content (possibly padded).
        assert!(result.paz_bytes.len() >= content_len);
        assert_eq!(&result.paz_bytes[..content_len], b"hello world");
        // PAZ length is aligned to 16.
        assert_eq!(result.paz_bytes.len() % PAZ_ALIGNMENT, 0);
    }

    // ── Single LZ4 entry ──

    #[test]
    fn test_single_lz4_entry() {
        let content = vec![0xABu8; 1024]; // repetitive data compresses well
        let content_len = content.len();

        let result = build_overlay(vec![OverlayInput {
            dir_path: "data".into(),
            filename: "big.bin".into(),
            content,
            compression_type: 2,
        }]);

        assert_eq!(result.entry_count, 1);
        // LZ4 compressed should be smaller than original.
        // The PAZ contains compressed data (+ padding), so it should be
        // smaller than the original 1024 bytes.
        assert!(result.paz_bytes.len() < content_len);

        // Verify we can decompress the PAZ content.
        // Find the compressed size from the PAMT file records.
        // We need to find file records section in PAMT.
        // For a single entry, the PAMT structure is deterministic.
    }

    // ── PAZ alignment ──

    #[test]
    fn test_paz_alignment_multiple_entries() {
        let inputs = vec![
            OverlayInput {
                dir_path: "dir".into(),
                filename: "a.bin".into(),
                content: vec![1u8; 7], // odd size
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "dir".into(),
                filename: "b.bin".into(),
                content: vec![2u8; 33], // odd size
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "dir".into(),
                filename: "c.bin".into(),
                content: vec![3u8; 16], // exact alignment
                compression_type: 0,
            },
        ];

        let result = build_overlay(inputs);
        assert_eq!(result.entry_count, 3);
        // Total PAZ length must be aligned.
        assert_eq!(result.paz_bytes.len() % PAZ_ALIGNMENT, 0);

        // Each entry must start at an aligned offset.
        // Entry 0 starts at 0 (aligned).
        // Entry 1 starts at align(7) = 16.
        assert_eq!(result.paz_bytes[0], 1);  // entry 0 content
        assert_eq!(result.paz_bytes[16], 2); // entry 1 content at offset 16
        // Entry 2 starts at 16 + align(33) = 16 + 48 = 64.
        assert_eq!(result.paz_bytes[64], 3); // entry 2 content at offset 64
    }

    // ── PAMT structure ──

    #[test]
    fn test_pamt_header_fields() {
        let result = build_overlay(vec![OverlayInput {
            dir_path: "data".into(),
            filename: "test.bin".into(),
            content: vec![0u8; 10],
            compression_type: 0,
        }]);

        let pamt = &result.pamt_bytes;

        // [0:4] outer_hash (non-zero after patching)
        let outer_hash = read_u32(pamt, 0);
        assert_ne!(outer_hash, 0);

        // [4:8] paz_count = 1
        assert_eq!(read_u32(pamt, 4), 1);

        // [8:12] constant
        assert_eq!(read_u32(pamt, 8), PAMT_CONSTANT);

        // [12:16] zero
        assert_eq!(read_u32(pamt, 12), 0);

        // [16:20] PAZ CRC
        let paz_crc = read_u32(pamt, 16);
        let expected_crc = hashlittle(&result.paz_bytes, HASH_SEED);
        assert_eq!(paz_crc, expected_crc);

        // [20:24] PAZ data length
        assert_eq!(read_u32(pamt, 20) as usize, result.paz_bytes.len());
    }

    #[test]
    fn test_pamt_folder_section() {
        let result = build_overlay(vec![OverlayInput {
            dir_path: "gamedata/binary__/client".into(),
            filename: "test.bin".into(),
            content: vec![0u8; 4],
            compression_type: 0,
        }]);

        let pamt = &result.pamt_bytes;
        let off = 24; // after header (6 u32s)

        // Folder section length.
        let folder_len = read_u32(pamt, off) as usize;
        assert!(folder_len > 0);

        // Parse folder entries within the section.
        let folder_start = off + 4;
        let folder_end = folder_start + folder_len;

        // First entry should be root "gamedata" with parent 0xFFFFFFFF.
        let parent0 = read_u32(pamt, folder_start);
        assert_eq!(parent0, 0xFFFF_FFFF);
        let name0_len = pamt[folder_start + 4] as usize;
        let name0 = std::str::from_utf8(&pamt[folder_start + 5..folder_start + 5 + name0_len])
            .unwrap();
        assert_eq!(name0, "gamedata");

        // Second entry "/binary__" should reference the first.
        let entry1_off = folder_start + 5 + name0_len;
        let parent1 = read_u32(pamt, entry1_off);
        assert_eq!(parent1, 0); // offset of root entry
        let name1_len = pamt[entry1_off + 4] as usize;
        let name1 =
            std::str::from_utf8(&pamt[entry1_off + 5..entry1_off + 5 + name1_len]).unwrap();
        assert_eq!(name1, "/binary__");

        // Third entry "/client".
        let entry2_off = entry1_off + 5 + name1_len;
        assert!(entry2_off < folder_end);
        let name2_len = pamt[entry2_off + 4] as usize;
        let name2 =
            std::str::from_utf8(&pamt[entry2_off + 5..entry2_off + 5 + name2_len]).unwrap();
        assert_eq!(name2, "/client");
    }

    #[test]
    fn test_pamt_node_section() {
        let result = build_overlay(vec![
            OverlayInput {
                dir_path: "data".into(),
                filename: "alpha.bin".into(),
                content: vec![0u8; 4],
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "data".into(),
                filename: "beta.bin".into(),
                content: vec![0u8; 4],
                compression_type: 0,
            },
        ]);

        let pamt = &result.pamt_bytes;
        let mut off = 24;

        // Skip folder section.
        let folder_len = read_u32(pamt, off) as usize;
        off += 4 + folder_len;

        // Node section.
        let node_len = read_u32(pamt, off) as usize;
        off += 4;
        let node_start = off;

        // First node: "alpha.bin" (sorted).
        let parent0 = read_u32(pamt, off);
        assert_eq!(parent0, 0xFFFF_FFFF);
        let name0_len = pamt[off + 4] as usize;
        let name0 =
            std::str::from_utf8(&pamt[off + 5..off + 5 + name0_len]).unwrap();
        assert_eq!(name0, "alpha.bin");

        // Second node: "beta.bin".
        off += 5 + name0_len;
        let parent1 = read_u32(pamt, off);
        assert_eq!(parent1, 0xFFFF_FFFF);
        let name1_len = pamt[off + 4] as usize;
        let name1 =
            std::str::from_utf8(&pamt[off + 5..off + 5 + name1_len]).unwrap();
        assert_eq!(name1, "beta.bin");

        assert_eq!(off + 5 + name1_len, node_start + node_len);
    }

    // ── Hash chain ──

    #[test]
    fn test_hash_chain() {
        let result = build_overlay(vec![OverlayInput {
            dir_path: "data".into(),
            filename: "file.bin".into(),
            content: vec![0xFFu8; 100],
            compression_type: 0,
        }]);

        let pamt = &result.pamt_bytes;

        // Verify PAZ CRC at offset 16.
        let stored_paz_crc = read_u32(pamt, 16);
        let computed_paz_crc = hashlittle(&result.paz_bytes, HASH_SEED);
        assert_eq!(stored_paz_crc, computed_paz_crc);

        // Verify outer hash at offset 0.
        let stored_outer = read_u32(pamt, 0);
        let computed_outer = hashlittle(&pamt[12..], HASH_SEED);
        assert_eq!(stored_outer, computed_outer);
    }

    #[test]
    fn test_hash_chain_changes_with_content() {
        let result_a = build_overlay(vec![OverlayInput {
            dir_path: "d".into(),
            filename: "f.bin".into(),
            content: vec![0x00u8; 32],
            compression_type: 0,
        }]);
        let result_b = build_overlay(vec![OverlayInput {
            dir_path: "d".into(),
            filename: "f.bin".into(),
            content: vec![0xFFu8; 32],
            compression_type: 0,
        }]);

        // PAZ CRCs should differ.
        let crc_a = read_u32(&result_a.pamt_bytes, 16);
        let crc_b = read_u32(&result_b.pamt_bytes, 16);
        assert_ne!(crc_a, crc_b);

        // Outer hashes should also differ.
        let outer_a = read_u32(&result_a.pamt_bytes, 0);
        let outer_b = read_u32(&result_b.pamt_bytes, 0);
        assert_ne!(outer_a, outer_b);
    }

    // ── Multiple entries from different directories ──

    #[test]
    fn test_multiple_directories() {
        let result = build_overlay(vec![
            OverlayInput {
                dir_path: "ui/hud".into(),
                filename: "health.xml".into(),
                content: b"<hp>100</hp>".to_vec(),
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "data/config".into(),
                filename: "settings.bin".into(),
                content: vec![1, 2, 3, 4],
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "ui/hud".into(),
                filename: "mana.xml".into(),
                content: b"<mp>50</mp>".to_vec(),
                compression_type: 0,
            },
        ]);

        assert_eq!(result.entry_count, 3);

        // All three entries should be present in PAZ.
        assert!(result.paz_bytes.len() >= 12 + 4 + 11); // minimum content

        // PAMT should have folder records for two unique top-level dirs
        // ("data/config" and "ui/hud", sorted).
        let pamt = &result.pamt_bytes;
        let mut off = 24;

        // Skip folder section.
        let folder_len = read_u32(pamt, off) as usize;
        off += 4 + folder_len;

        // Skip node section.
        let node_len = read_u32(pamt, off) as usize;
        off += 4 + node_len;

        // Folder record count: "data", "data/config", "ui", "ui/hud" — but
        // unique_dirs are "data/config" and "ui/hud", and each intermediate
        // component is created in the folder section, but folder *records*
        // are only written for the unique_dirs themselves.
        let folder_count = read_u32(pamt, off);
        assert_eq!(folder_count, 2); // "data/config" and "ui/hud"
        off += 4;

        // Skip folder records (16 bytes each).
        off += folder_count as usize * 16;

        // File record count.
        let file_count = read_u32(pamt, off);
        assert_eq!(file_count, 3);
    }

    // ── Sort order within directory ──

    #[test]
    fn test_sort_order_within_directory() {
        // Insert in reverse alphabetical order.
        let result = build_overlay(vec![
            OverlayInput {
                dir_path: "dir".into(),
                filename: "zebra.bin".into(),
                content: vec![3],
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "dir".into(),
                filename: "apple.bin".into(),
                content: vec![1],
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "dir".into(),
                filename: "mango.bin".into(),
                content: vec![2],
                compression_type: 0,
            },
        ]);

        let pamt = &result.pamt_bytes;
        let mut off = 24;

        // Skip folder section.
        let folder_len = read_u32(pamt, off) as usize;
        off += 4 + folder_len;

        // Node section — filenames should be sorted alphabetically.
        let node_len = read_u32(pamt, off) as usize;
        off += 4;

        let mut names: Vec<String> = Vec::new();
        let node_end = off + node_len;
        while off < node_end {
            off += 4; // skip parent
            let name_len = pamt[off] as usize;
            off += 1;
            let name = std::str::from_utf8(&pamt[off..off + name_len])
                .unwrap()
                .to_string();
            names.push(name);
            off += name_len;
        }

        assert_eq!(names, vec!["apple.bin", "mango.bin", "zebra.bin"]);
    }

    // ── DDS compression entry ──

    #[test]
    fn test_dds_compression_entry() {
        // Build a minimal DDS file (128-byte header + body).
        let mut dds_data = vec![0u8; 256];
        dds_data[0..4].copy_from_slice(b"DDS ");
        dds_data[84..88].copy_from_slice(b"DXT1");
        dds_data[12..16].copy_from_slice(&64u32.to_le_bytes()); // height
        dds_data[16..20].copy_from_slice(&64u32.to_le_bytes()); // width
        dds_data[28..32].copy_from_slice(&1u32.to_le_bytes());  // mips

        let result = build_overlay(vec![OverlayInput {
            dir_path: "textures".into(),
            filename: "test.dds".into(),
            content: dds_data.clone(),
            compression_type: 1,
        }]);

        assert_eq!(result.entry_count, 1);
        // DDS split: header (128) + LZ4 body, padded to full original size.
        // The PAZ should contain the payload aligned to 16.
        assert!(result.paz_bytes.len() >= 128);
        // First 4 bytes should be the DDS magic.
        assert_eq!(&result.paz_bytes[..4], b"DDS ");
    }

    // ── File records contain correct sizes and flags ──

    #[test]
    fn test_file_records_correctness() {
        let content = vec![0xABu8; 200];
        let content_len = content.len() as u32;

        let result = build_overlay(vec![OverlayInput {
            dir_path: "d".into(),
            filename: "f.bin".into(),
            content,
            compression_type: 0,
        }]);

        let pamt = &result.pamt_bytes;
        let mut off = 24;

        // Skip folder section.
        let folder_len = read_u32(pamt, off) as usize;
        off += 4 + folder_len;

        // Skip node section.
        let node_len = read_u32(pamt, off) as usize;
        off += 4 + node_len;

        // Skip folder records.
        let folder_count = read_u32(pamt, off) as usize;
        off += 4 + folder_count * 16;

        // File records.
        let file_count = read_u32(pamt, off);
        assert_eq!(file_count, 1);
        off += 4;

        // node_ref(4) + offset(4) + comp(4) + decomp(4) + flags(4)
        let _node_ref = read_u32(pamt, off);
        let paz_offset = read_u32(pamt, off + 4);
        let comp_size = read_u32(pamt, off + 8);
        let decomp_size = read_u32(pamt, off + 12);
        let flags = read_u32(pamt, off + 16);

        assert_eq!(paz_offset, 0);
        assert_eq!(comp_size, content_len);
        assert_eq!(decomp_size, content_len);
        assert_eq!(flags, 0); // uncompressed: (0 << 16) = 0
    }

    #[test]
    fn test_lz4_file_record_sizes() {
        let content = vec![0xCDu8; 512];
        let original_len = content.len() as u32;

        let result = build_overlay(vec![OverlayInput {
            dir_path: "d".into(),
            filename: "f.bin".into(),
            content,
            compression_type: 2,
        }]);

        let pamt = &result.pamt_bytes;
        let mut off = 24;

        // Skip to file records.
        let folder_len = read_u32(pamt, off) as usize;
        off += 4 + folder_len;
        let node_len = read_u32(pamt, off) as usize;
        off += 4 + node_len;
        let folder_count = read_u32(pamt, off) as usize;
        off += 4 + folder_count * 16;

        let file_count = read_u32(pamt, off);
        assert_eq!(file_count, 1);
        off += 4;

        let comp_size = read_u32(pamt, off + 8);
        let decomp_size = read_u32(pamt, off + 12);
        let flags = read_u32(pamt, off + 16);

        // LZ4 compressed size should be less than original for repetitive data.
        assert!(comp_size < original_len);
        assert_eq!(decomp_size, original_len);
        // flags = (2 << 16) = 0x0002_0000 — compression_type 2 in bits 16-19
        assert_eq!(flags, 0x0002_0000);
    }

    // ── Round-trip integration test: overlay builder → PAMT parser ──

    #[test]
    fn test_roundtrip_overlay_to_parser() {
        use crate::paz::parse_pamt;

        let result = build_overlay(vec![
            OverlayInput {
                dir_path: "gamedata".into(),
                filename: "inventory.pabgb".into(),
                content: vec![0xABu8; 200],
                compression_type: 0,
            },
            OverlayInput {
                dir_path: "gamedata".into(),
                filename: "config.xml".into(),
                content: vec![0xCDu8; 512],
                compression_type: 2,
            },
        ]);

        // Parse the PAMT we just built
        let entries = parse_pamt(&result.pamt_bytes)
            .expect("overlay PAMT should be parseable");

        assert_eq!(entries.len(), 2);

        // Entries should be sorted by filename within dir
        assert!(entries[0].path.ends_with("config.xml"));
        assert!(entries[1].path.ends_with("inventory.pabgb"));

        // config.xml: LZ4 compressed → compression_type = 2
        assert_eq!(entries[0].compression_type(), 2);
        assert!(entries[0].is_compressed());

        // inventory.pabgb: uncompressed → compression_type = 0
        assert_eq!(entries[1].compression_type(), 0);
        assert!(!entries[1].is_compressed());

        // Both should have paz_index = 0 (overlay has single PAZ)
        assert_eq!(entries[0].paz_index, 0);
        assert_eq!(entries[1].paz_index, 0);

        // Offsets should be valid (within PAZ bounds)
        for e in &entries {
            assert!((e.offset as usize + e.comp_size as usize) <= result.paz_bytes.len());
        }
    }
}
