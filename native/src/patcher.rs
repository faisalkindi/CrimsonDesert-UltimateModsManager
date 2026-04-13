//! Byte patching and pattern scanning for Crimson Desert mod files.
//!
//! Ported from `json_patch_handler.py`. Two main operations:
//!
//! - [`pattern_scan`]: Two-tier relocator that finds where original bytes
//!   moved to after a game update (contextual fingerprint, then simple scan).
//! - [`apply_byte_patches`]: Applies a list of replace/insert byte changes
//!   to decompressed file data, with optional signature-relative offsets
//!   and automatic relocation via pattern scan.

// ── Types ──────────────────────────────────────────────────────────────

/// Whether a byte change replaces existing bytes or inserts new ones.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChangeType {
    Replace,
    Insert,
}

/// A single byte-level change to apply to decompressed file data.
#[derive(Debug, Clone)]
pub struct ByteChange {
    /// Offset into the decompressed data (absolute, or relative to
    /// signature end when a signature is provided).
    pub offset: usize,
    /// The original bytes expected at `offset` (used for verification
    /// and pattern-scan relocation).
    pub original: Vec<u8>,
    /// The replacement bytes (Replace) or bytes to insert (Insert).
    pub patched: Vec<u8>,
    /// Replace existing bytes in-place, or insert before `offset`.
    pub change_type: ChangeType,
}

// ── Pattern scan ───────────────────────────────────────────────────────

/// Find all occurrences of `needle` in `haystack`.
fn find_all(haystack: &[u8], needle: &[u8]) -> Vec<usize> {
    if needle.is_empty() || needle.len() > haystack.len() {
        return Vec::new();
    }
    let mut matches = Vec::new();
    let mut start = 0;
    while start + needle.len() <= haystack.len() {
        if let Some(pos) = haystack[start..].windows(needle.len()).position(|w| w == needle) {
            let abs = start + pos;
            matches.push(abs);
            start = abs + 1;
        } else {
            break;
        }
    }
    matches
}

/// Find the relocated position of `original` bytes in `data`.
///
/// Two-tier approach (matching the Python engine):
///
/// **Tier 1 — Contextual scan** (requires `vanilla`):
/// Grab a context window from the vanilla reference around `original_offset`,
/// then search `data` for that unique fingerprint.  Try window sizes
/// 24, 16, 12, 8 in order.  If exactly one match is found, return the
/// relocated offset.  Zero or multiple matches fall through to the next
/// smaller window size.
///
/// **Tier 2 — Simple scan:**
/// Search for `original` bytes directly.  Short patterns (< 4 bytes)
/// are limited to a +/-512-byte window around `original_offset` to
/// prevent false positives.  Longer patterns search the entire file.
/// The closest match to `original_offset` wins.
///
/// Returns `Some(new_offset)` or `None` if not found / ambiguous.
pub fn pattern_scan(
    data: &[u8],
    original_offset: usize,
    original: &[u8],
    vanilla: Option<&[u8]>,
) -> Option<usize> {
    // ── Tier 1: Contextual scan ────────────────────────────────────
    if let Some(van) = vanilla {
        if original_offset < van.len() {
            for ctx_size in [24usize, 16, 12, 8] {
                let ctx_start = original_offset.saturating_sub(ctx_size);
                let ctx_end = (original_offset + original.len() + ctx_size).min(van.len());
                if ctx_end - ctx_start < ctx_size {
                    continue;
                }
                let context = &van[ctx_start..ctx_end];
                let patch_rel = original_offset - ctx_start;

                let matches = find_all(data, context);

                if matches.len() == 1 {
                    let new_offset = matches[0] + patch_rel;
                    if new_offset + original.len() <= data.len() {
                        return Some(new_offset);
                    }
                }
                // 0 matches = context changed, >1 = ambiguous; try smaller window
            }
        }
    }

    // ── Tier 2: Simple pattern scan ────────────────────────────────
    let (scan_start, scan_end) = if original.len() < 4 {
        // Short patterns: ±512 bytes only
        let window = 512;
        let start = original_offset.saturating_sub(window);
        let end = (original_offset + window).min(data.len());
        (start, end)
    } else {
        (0, data.len())
    };

    let region = &data[scan_start..scan_end];
    let matches = find_all(region, original);

    let mut best_match: Option<usize> = None;
    let mut best_dist = usize::MAX;
    for &pos in &matches {
        let abs_pos = scan_start + pos;
        let dist = if abs_pos >= original_offset {
            abs_pos - original_offset
        } else {
            original_offset - abs_pos
        };
        if dist < best_dist {
            best_dist = dist;
            best_match = Some(abs_pos);
        }
    }

    // Only return if it actually relocated (different from original)
    match best_match {
        Some(m) if m != original_offset => Some(m),
        _ => None,
    }
}

// ── Byte patching ──────────────────────────────────────────────────────

/// Apply byte patches to decompressed file data.
///
/// Each element of `changes` describes a single byte-level modification.
///
/// If `signature` is provided, the function searches `data` for that byte
/// sequence and treats every change offset as relative to the **end** of
/// the first match.  If the signature is not found, no patches are applied
/// and `(0, 0, 0)` is returned.
///
/// If `vanilla` is provided, mismatched replaces are fed through
/// [`pattern_scan`] to attempt automatic relocation.
///
/// Processing order:
/// 1. **Replaces** — applied in the order given (in-place, no size change).
/// 2. **Inserts** — sorted by offset descending so that each insertion
///    does not shift positions of later inserts.
///
/// Returns `(applied, mismatched, relocated)`.
pub fn apply_byte_patches(
    data: &mut Vec<u8>,
    changes: &[ByteChange],
    signature: Option<&[u8]>,
    vanilla: Option<&[u8]>,
) -> (u32, u32, u32) {
    let mut applied: u32 = 0;
    let mut mismatched: u32 = 0;
    let mut relocated: u32 = 0;

    // Resolve signature base offset
    let base_offset: usize = match signature {
        Some(sig) => {
            let positions = find_all(data, sig);
            if positions.is_empty() {
                return (0, 0, 0);
            }
            positions[0] + sig.len()
        }
        None => 0,
    };

    // Partition into replaces and inserts
    let mut replaces: Vec<&ByteChange> = Vec::new();
    let mut inserts: Vec<&ByteChange> = Vec::new();
    for change in changes {
        match change.change_type {
            ChangeType::Replace => replaces.push(change),
            ChangeType::Insert => inserts.push(change),
        }
    }

    // Phase 1: Replaces (in order, position-stable)
    for change in &replaces {
        let offset = base_offset + change.offset;

        if offset + change.patched.len() > data.len() {
            continue; // patch exceeds file size
        }

        // Verify original bytes match
        if !change.original.is_empty() {
            if offset + change.original.len() > data.len() {
                mismatched += 1;
                continue;
            }
            let actual = &data[offset..offset + change.original.len()];
            if actual != change.original.as_slice() {
                // Check if already patched
                if offset + change.patched.len() <= data.len()
                    && data[offset..offset + change.patched.len()] == change.patched[..]
                {
                    applied += 1;
                    continue;
                }

                // Attempt pattern-scan relocation
                if let Some(new_offset) =
                    pattern_scan(data, offset, &change.original, vanilla)
                {
                    if new_offset + change.original.len() <= data.len()
                        && data[new_offset..new_offset + change.original.len()]
                            == change.original[..]
                    {
                        let end = new_offset + change.patched.len();
                        data[new_offset..end].copy_from_slice(&change.patched);
                        applied += 1;
                        relocated += 1;
                        continue;
                    }
                }

                mismatched += 1;
                continue;
            }
        }

        // Apply the patch
        let end = offset + change.patched.len();
        data[offset..end].copy_from_slice(&change.patched);
        applied += 1;
    }

    // Phase 2: Inserts (reverse offset order to preserve positions)
    inserts.sort_by(|a, b| b.offset.cmp(&a.offset));
    for change in &inserts {
        let offset = base_offset + change.offset;
        if offset <= data.len() {
            data.splice(offset..offset, change.patched.iter().copied());
            applied += 1;
        }
    }

    (applied, mismatched, relocated)
}

// ── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── pattern_scan tests ─────────────────────────────────────────

    #[test]
    fn test_pattern_scan_no_vanilla_exact_match() {
        // Pattern at its original position => no relocation (returns None)
        let data = b"\x00\x01\x02\xAA\xBB\xCC\x03\x04";
        let result = pattern_scan(data, 3, &[0xAA, 0xBB, 0xCC], None);
        assert_eq!(result, None, "exact position should not count as relocated");
    }

    #[test]
    fn test_pattern_scan_simple_shifted() {
        // Original at offset 2, but data has 4 bytes prepended so it's at 6
        let vanilla = b"\x00\x00\xAA\xBB\xCC\x00\x00";
        let data = b"\xFF\xFF\xFF\xFF\x00\x00\xAA\xBB\xCC\x00\x00";
        let result = pattern_scan(data, 2, &[0xAA, 0xBB, 0xCC], Some(vanilla.as_slice()));
        assert_eq!(result, Some(6));
    }

    #[test]
    fn test_pattern_scan_contextual_unique() {
        // Contextual fingerprint is unique => tier 1 succeeds
        let vanilla: Vec<u8> = {
            let mut v = vec![0x10, 0x20, 0x30];
            v.extend_from_slice(&[0xDE, 0xAD]); // offset 3
            v.extend_from_slice(&[0x40, 0x50, 0x60]);
            v
        };
        // Data has the same surrounding bytes but shifted by +5
        let data: Vec<u8> = {
            let mut v = vec![0xFF; 5];
            v.extend_from_slice(&vanilla);
            v
        };
        let result = pattern_scan(&data, 3, &[0xDE, 0xAD], Some(&vanilla));
        assert_eq!(result, Some(8)); // 3 + 5
    }

    #[test]
    fn test_pattern_scan_ambiguous_context_falls_to_simple() {
        // Context is duplicated so tier 1 is ambiguous; tier 2 picks closest
        let block = [0xAA, 0xBB, 0xCC, 0xDD];
        let mut data = Vec::new();
        data.extend_from_slice(&block); // offset 0
        data.extend_from_slice(&[0x00; 10]);
        data.extend_from_slice(&block); // offset 14
        // Original offset was 0, closest is 0 (same position => None),
        // so try original offset 14 with closest match at 14 => None too.
        // Instead, test where original_offset doesn't match either:
        // Original offset 7 — closest match is 0 (dist 7) vs 14 (dist 7), both equal;
        // implementation picks whichever appears first with dist < best_dist, so 0 wins.
        let result = pattern_scan(&data, 7, &block, None);
        assert_eq!(result, Some(0)); // closest (first found with min dist)
    }

    #[test]
    fn test_pattern_scan_short_pattern_window_limit() {
        // A 2-byte pattern at offset 0 should not match at offset 1000
        let mut data = vec![0x00; 2000];
        data[0] = 0xAA;
        data[1] = 0xBB;
        // Also place the pattern far away at 1000
        data[1000] = 0xAA;
        data[1001] = 0xBB;

        // Searching with original_offset=0: within ±512 only the first match at 0
        // qualifies, but offset 0 == original_offset => no relocation
        let result = pattern_scan(&data, 0, &[0xAA, 0xBB], None);
        assert_eq!(result, None, "exact position, no relocation");

        // Searching with original_offset=1000: window is [488..1512],
        // only the match at 1000 is in range => same position => None
        let result2 = pattern_scan(&data, 1000, &[0xAA, 0xBB], None);
        assert_eq!(result2, None);

        // Searching with original_offset=5: window is [0..517],
        // match at 0 is in range and != 5 => relocation found
        let result3 = pattern_scan(&data, 5, &[0xAA, 0xBB], None);
        assert_eq!(result3, Some(0));
    }

    #[test]
    fn test_pattern_scan_long_pattern_full_file() {
        // A 4-byte pattern searches the entire file
        let mut data = vec![0x00; 2000];
        data[1500] = 0xAA;
        data[1501] = 0xBB;
        data[1502] = 0xCC;
        data[1503] = 0xDD;

        let result = pattern_scan(&data, 100, &[0xAA, 0xBB, 0xCC, 0xDD], None);
        assert_eq!(result, Some(1500));
    }

    #[test]
    fn test_pattern_scan_no_match() {
        let data = b"\x00\x01\x02\x03\x04";
        let result = pattern_scan(data, 0, &[0xFF, 0xFE], None);
        assert_eq!(result, None);
    }

    // ── apply_byte_patches: basic replace ──────────────────────────

    #[test]
    fn test_basic_replace() {
        let mut data = vec![0x00, 0x01, 0x02, 0x03, 0x04, 0x05];
        let changes = vec![ByteChange {
            offset: 2,
            original: vec![0x02, 0x03],
            patched: vec![0xFF, 0xFE],
            change_type: ChangeType::Replace,
        }];
        let (applied, mismatched, relocated) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 1);
        assert_eq!(mismatched, 0);
        assert_eq!(relocated, 0);
        assert_eq!(data, vec![0x00, 0x01, 0xFF, 0xFE, 0x04, 0x05]);
    }

    #[test]
    fn test_replace_original_mismatch_no_scan() {
        let mut data = vec![0x00, 0x01, 0x99, 0x03, 0x04, 0x05];
        let changes = vec![ByteChange {
            offset: 2,
            original: vec![0x02, 0x03], // doesn't match 0x99, 0x03
            patched: vec![0xFF, 0xFE],
            change_type: ChangeType::Replace,
        }];
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 0);
        assert_eq!(mismatched, 1);
        // data unchanged
        assert_eq!(data[2], 0x99);
    }

    #[test]
    fn test_replace_already_patched() {
        let mut data = vec![0x00, 0x01, 0xFF, 0xFE, 0x04, 0x05];
        let changes = vec![ByteChange {
            offset: 2,
            original: vec![0x02, 0x03],
            patched: vec![0xFF, 0xFE], // matches current data at offset 2
            change_type: ChangeType::Replace,
        }];
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 1, "already-patched should count as applied");
        assert_eq!(mismatched, 0);
    }

    #[test]
    fn test_replace_no_original_verification() {
        // Empty original => no verification, just overwrite
        let mut data = vec![0x00, 0x01, 0x02, 0x03];
        let changes = vec![ByteChange {
            offset: 1,
            original: vec![],
            patched: vec![0xAA, 0xBB],
            change_type: ChangeType::Replace,
        }];
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 1);
        assert_eq!(mismatched, 0);
        assert_eq!(data, vec![0x00, 0xAA, 0xBB, 0x03]);
    }

    #[test]
    fn test_multiple_replaces() {
        let mut data = vec![0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07];
        let changes = vec![
            ByteChange {
                offset: 0,
                original: vec![0x00],
                patched: vec![0xAA],
                change_type: ChangeType::Replace,
            },
            ByteChange {
                offset: 4,
                original: vec![0x04, 0x05],
                patched: vec![0xBB, 0xCC],
                change_type: ChangeType::Replace,
            },
        ];
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 2);
        assert_eq!(mismatched, 0);
        assert_eq!(data, vec![0xAA, 0x01, 0x02, 0x03, 0xBB, 0xCC, 0x06, 0x07]);
    }

    // ── apply_byte_patches: pattern scan relocation ────────────────

    #[test]
    fn test_replace_with_relocation() {
        // Vanilla has 0xDE, 0xAD at offset 2.
        // Current data has 4 extra bytes prepended, so 0xDE, 0xAD is at offset 6.
        let vanilla = vec![0x10, 0x20, 0xDE, 0xAD, 0x30, 0x40];
        let mut data = vec![0xFF, 0xFF, 0xFF, 0xFF, 0x10, 0x20, 0xDE, 0xAD, 0x30, 0x40];

        let changes = vec![ByteChange {
            offset: 2,
            original: vec![0xDE, 0xAD],
            patched: vec![0xBE, 0xEF],
            change_type: ChangeType::Replace,
        }];
        let (applied, mismatched, relocated) =
            apply_byte_patches(&mut data, &changes, None, Some(&vanilla));
        assert_eq!(applied, 1);
        assert_eq!(mismatched, 0);
        assert_eq!(relocated, 1);
        assert_eq!(data[6], 0xBE);
        assert_eq!(data[7], 0xEF);
    }

    // ── apply_byte_patches: signature-relative offsets ─────────────

    #[test]
    fn test_signature_relative_offsets() {
        // Signature "ABCD" at offset 4; patches relative to end of signature (offset 8)
        let mut data = vec![
            0x00, 0x01, 0x02, 0x03, // 0-3: prefix
            0x41, 0x42, 0x43, 0x44, // 4-7: "ABCD"
            0x10, 0x20, 0x30, 0x40, // 8-11: post-signature data
        ];
        let sig = b"ABCD";
        let changes = vec![ByteChange {
            offset: 0, // relative to end of sig => absolute 8
            original: vec![0x10, 0x20],
            patched: vec![0xAA, 0xBB],
            change_type: ChangeType::Replace,
        }];
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, Some(sig), None);
        assert_eq!(applied, 1);
        assert_eq!(mismatched, 0);
        assert_eq!(data[8], 0xAA);
        assert_eq!(data[9], 0xBB);
    }

    #[test]
    fn test_signature_not_found() {
        let mut data = vec![0x00, 0x01, 0x02, 0x03];
        let changes = vec![ByteChange {
            offset: 0,
            original: vec![0x00],
            patched: vec![0xFF],
            change_type: ChangeType::Replace,
        }];
        let sig = b"\xDE\xAD\xBE\xEF";
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, Some(sig), None);
        assert_eq!(applied, 0, "no patches when signature missing");
        assert_eq!(mismatched, 0);
        assert_eq!(data[0], 0x00, "data unchanged");
    }

    #[test]
    fn test_signature_with_multiple_changes() {
        let mut data = vec![
            0x00, 0x00,             // 0-1: padding
            0xCA, 0xFE,             // 2-3: signature
            0xAA, 0xBB, 0xCC, 0xDD, // 4-7: data (offsets 0-3 relative to sig end)
        ];
        let sig = b"\xCA\xFE";
        let changes = vec![
            ByteChange {
                offset: 0,
                original: vec![0xAA],
                patched: vec![0x11],
                change_type: ChangeType::Replace,
            },
            ByteChange {
                offset: 2,
                original: vec![0xCC],
                patched: vec![0x33],
                change_type: ChangeType::Replace,
            },
        ];
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, Some(sig), None);
        assert_eq!(applied, 2);
        assert_eq!(mismatched, 0);
        assert_eq!(data, vec![0x00, 0x00, 0xCA, 0xFE, 0x11, 0xBB, 0x33, 0xDD]);
    }

    // ── apply_byte_patches: insert operations ──────────────────────

    #[test]
    fn test_basic_insert() {
        let mut data = vec![0x00, 0x01, 0x02, 0x03];
        let changes = vec![ByteChange {
            offset: 2,
            original: vec![],
            patched: vec![0xAA, 0xBB],
            change_type: ChangeType::Insert,
        }];
        let (applied, mismatched, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 1);
        assert_eq!(mismatched, 0);
        assert_eq!(data, vec![0x00, 0x01, 0xAA, 0xBB, 0x02, 0x03]);
    }

    #[test]
    fn test_insert_at_end() {
        let mut data = vec![0x00, 0x01];
        let changes = vec![ByteChange {
            offset: 2,
            original: vec![],
            patched: vec![0xFF],
            change_type: ChangeType::Insert,
        }];
        let (applied, _, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 1);
        assert_eq!(data, vec![0x00, 0x01, 0xFF]);
    }

    #[test]
    fn test_multiple_inserts_reverse_order() {
        // Two inserts: offset 1 and offset 3. Must apply in reverse order.
        let mut data = vec![0x00, 0x01, 0x02, 0x03];
        let changes = vec![
            ByteChange {
                offset: 1,
                original: vec![],
                patched: vec![0xAA],
                change_type: ChangeType::Insert,
            },
            ByteChange {
                offset: 3,
                original: vec![],
                patched: vec![0xBB],
                change_type: ChangeType::Insert,
            },
        ];
        let (applied, _, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 2);
        // Insert 0xBB at 3 first: [0x00, 0x01, 0x02, 0xBB, 0x03]
        // Insert 0xAA at 1 next:  [0x00, 0xAA, 0x01, 0x02, 0xBB, 0x03]
        assert_eq!(data, vec![0x00, 0xAA, 0x01, 0x02, 0xBB, 0x03]);
    }

    // ── apply_byte_patches: mixed replaces and inserts ─────────────

    #[test]
    fn test_mixed_replace_and_insert() {
        let mut data = vec![0x00, 0x01, 0x02, 0x03, 0x04];
        let changes = vec![
            ByteChange {
                offset: 1,
                original: vec![0x01],
                patched: vec![0xAA],
                change_type: ChangeType::Replace,
            },
            ByteChange {
                offset: 3,
                original: vec![],
                patched: vec![0xBB, 0xCC],
                change_type: ChangeType::Insert,
            },
        ];
        let (applied, _, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 2);
        // Replace first: [0x00, 0xAA, 0x02, 0x03, 0x04]
        // Insert at 3:   [0x00, 0xAA, 0x02, 0xBB, 0xCC, 0x03, 0x04]
        assert_eq!(data, vec![0x00, 0xAA, 0x02, 0xBB, 0xCC, 0x03, 0x04]);
    }

    // ── apply_byte_patches: edge cases ─────────────────────────────

    #[test]
    fn test_patch_exceeds_file_size() {
        let mut data = vec![0x00, 0x01];
        let changes = vec![ByteChange {
            offset: 1,
            original: vec![0x01],
            patched: vec![0xAA, 0xBB, 0xCC], // needs 3 bytes from offset 1, but data is only 2
            change_type: ChangeType::Replace,
        }];
        let (applied, _, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 0, "should skip patch that exceeds file size");
        assert_eq!(data, vec![0x00, 0x01]);
    }

    #[test]
    fn test_empty_changes() {
        let mut data = vec![0x00, 0x01, 0x02];
        let changes: Vec<ByteChange> = vec![];
        let (applied, mismatched, relocated) =
            apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 0);
        assert_eq!(mismatched, 0);
        assert_eq!(relocated, 0);
        assert_eq!(data, vec![0x00, 0x01, 0x02]);
    }

    #[test]
    fn test_insert_beyond_length_exactly() {
        // Insert at exactly data.len() (append)
        let mut data = vec![0x00];
        let changes = vec![ByteChange {
            offset: 1,
            original: vec![],
            patched: vec![0xFF],
            change_type: ChangeType::Insert,
        }];
        let (applied, _, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 1);
        assert_eq!(data, vec![0x00, 0xFF]);
    }

    #[test]
    fn test_insert_beyond_length_rejected() {
        // Insert past data.len() should be skipped
        let mut data = vec![0x00];
        let changes = vec![ByteChange {
            offset: 5,
            original: vec![],
            patched: vec![0xFF],
            change_type: ChangeType::Insert,
        }];
        let (applied, _, _) = apply_byte_patches(&mut data, &changes, None, None);
        assert_eq!(applied, 0);
        assert_eq!(data, vec![0x00]);
    }

    // ── find_all helper tests ──────────────────────────────────────

    #[test]
    fn test_find_all_basic() {
        let data = b"\xAA\xBB\xCC\xAA\xBB\xDD\xAA\xBB";
        let matches = find_all(data, &[0xAA, 0xBB]);
        assert_eq!(matches, vec![0, 3, 6]);
    }

    #[test]
    fn test_find_all_overlapping() {
        let data = b"\xAA\xAA\xAA";
        let matches = find_all(data, &[0xAA, 0xAA]);
        assert_eq!(matches, vec![0, 1]);
    }

    #[test]
    fn test_find_all_no_match() {
        let data = b"\x00\x01\x02";
        let matches = find_all(data, &[0xFF]);
        assert_eq!(matches, Vec::<usize>::new());
    }

    #[test]
    fn test_find_all_empty_needle() {
        let data = b"\x00\x01\x02";
        let matches = find_all(data, &[]);
        assert_eq!(matches, Vec::<usize>::new());
    }

    #[test]
    fn test_find_all_needle_larger_than_haystack() {
        let data = b"\x00";
        let matches = find_all(data, &[0x00, 0x01, 0x02]);
        assert_eq!(matches, Vec::<usize>::new());
    }

    // ── Signature + relocation combined ────────────────────────────

    #[test]
    fn test_signature_plus_relocation() {
        // Signature at offset 0, data after signature shifted
        let sig = b"\xCA\xFE";
        let vanilla = vec![
            0xCA, 0xFE,             // signature
            0xDE, 0xAD, 0x11, 0x22, // post-sig data, target at relative offset 0
        ];
        // Data has the same signature but with 2 extra bytes inserted after sig
        let mut data = vec![
            0xCA, 0xFE,             // signature
            0x00, 0x00,             // 2 inserted bytes
            0xDE, 0xAD, 0x11, 0x22,
        ];
        let changes = vec![ByteChange {
            offset: 0, // relative to sig end => absolute 2, but data has 0x00 0x00 there
            original: vec![0xDE, 0xAD],
            patched: vec![0xBE, 0xEF],
            change_type: ChangeType::Replace,
        }];
        // Vanilla reference should help relocate 0xDE 0xAD from offset 2 to offset 4
        let (applied, mismatched, relocated) =
            apply_byte_patches(&mut data, &changes, Some(sig), Some(&vanilla));
        assert_eq!(applied, 1);
        assert_eq!(mismatched, 0);
        assert_eq!(relocated, 1);
        assert_eq!(data[4], 0xBE);
        assert_eq!(data[5], 0xEF);
    }
}
