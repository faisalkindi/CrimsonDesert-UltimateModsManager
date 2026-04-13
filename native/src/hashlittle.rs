//! Bob Jenkins' lookup3 hashlittle — returns the primary hash (c).
//!
//! Two variants:
//! - `hashlittle(data, initval)` — standard Jenkins lookup3 for key derivation + PAMT hashing
//! - `pa_checksum(data)` — Pearl Abyss modified variant for PAZ/PAMT/PAPGT CRC
//!
//! HASH_SEED = 0xC5EDE (used for PAMT/PAPGT integrity chain)

pub const HASH_SEED: u32 = 0x000C5EDE;

#[inline(always)]
fn rot(v: u32, k: u32) -> u32 {
    v.rotate_left(k)
}

/// Standard Bob Jenkins lookup3 hashlittle.
/// Returns the primary hash value (c).
pub fn hashlittle(data: &[u8], initval: u32) -> u32 {
    let length = data.len();
    let mut a: u32 = 0xDEADBEEF_u32.wrapping_add(length as u32).wrapping_add(initval);
    let mut b: u32 = a;
    let mut c: u32 = a;
    let mut off = 0;
    let mut remaining = length;

    // Process 12-byte chunks
    while remaining > 12 {
        a = a.wrapping_add(u32::from_le_bytes([data[off], data[off + 1], data[off + 2], data[off + 3]]));
        b = b.wrapping_add(u32::from_le_bytes([data[off + 4], data[off + 5], data[off + 6], data[off + 7]]));
        c = c.wrapping_add(u32::from_le_bytes([data[off + 8], data[off + 9], data[off + 10], data[off + 11]]));

        // mix
        a = a.wrapping_sub(c); a ^= rot(c, 4);  c = c.wrapping_add(b);
        b = b.wrapping_sub(a); b ^= rot(a, 6);  a = a.wrapping_add(c);
        c = c.wrapping_sub(b); c ^= rot(b, 8);  b = b.wrapping_add(a);
        a = a.wrapping_sub(c); a ^= rot(c, 16); c = c.wrapping_add(b);
        b = b.wrapping_sub(a); b ^= rot(a, 19); a = a.wrapping_add(c);
        c = c.wrapping_sub(b); c ^= rot(b, 4);  b = b.wrapping_add(a);

        off += 12;
        remaining -= 12;
    }

    // Handle tail bytes (0-12 remaining) — zero-padded to 12
    let mut tail = [0u8; 12];
    tail[..remaining].copy_from_slice(&data[off..off + remaining]);

    if remaining >= 12 {
        c = c.wrapping_add(u32::from_le_bytes([tail[8], tail[9], tail[10], tail[11]]));
    } else if remaining >= 9 {
        let v = u32::from_le_bytes([tail[8], tail[9], tail[10], tail[11]]);
        let mask = 0xFFFFFFFF_u32 >> (8 * (12 - remaining));
        c = c.wrapping_add(v & mask);
    }

    if remaining >= 8 {
        b = b.wrapping_add(u32::from_le_bytes([tail[4], tail[5], tail[6], tail[7]]));
    } else if remaining >= 5 {
        let v = u32::from_le_bytes([tail[4], tail[5], tail[6], tail[7]]);
        let mask = 0xFFFFFFFF_u32 >> (8 * (8 - remaining));
        b = b.wrapping_add(v & mask);
    }

    if remaining >= 4 {
        a = a.wrapping_add(u32::from_le_bytes([tail[0], tail[1], tail[2], tail[3]]));
    } else if remaining >= 1 {
        let v = u32::from_le_bytes([tail[0], tail[1], tail[2], tail[3]]);
        let mask = 0xFFFFFFFF_u32 >> (8 * (4 - remaining));
        a = a.wrapping_add(v & mask);
    } else {
        return c; // zero-length input
    }

    // final mix
    c ^= b; c = c.wrapping_sub(rot(b, 14));
    a ^= c; a = a.wrapping_sub(rot(c, 11));
    b ^= a; b = b.wrapping_sub(rot(a, 25));
    c ^= b; c = c.wrapping_sub(rot(b, 16));
    a ^= c; a = a.wrapping_sub(rot(c, 4));
    b ^= a; b = b.wrapping_sub(rot(a, 14));
    c ^= b; c = c.wrapping_sub(rot(b, 24));

    c
}

/// Pearl Abyss checksum for PAZ CRC, PAMT header CRC, PAPGT CRC.
///
/// Verified against MrIkso/CrimsonDesertTools PaChecksum.cs:
/// this is algebraically identical to `hashlittle(data, HASH_SEED)`.
///
/// The C# source uses `(length - 0x2145E233)` as init, but
/// `-0x2145E233 ≡ 0xDEADBEEF + 0xC5EDE (mod 2^32)` — same constant.
/// The C# final mix uses RotateRight(7)/RotateRight(8), but
/// `rot_right(7) = rot_left(25)` and `rot_right(8) = rot_left(24)` —
/// same operations as the standard Jenkins final().
#[inline]
pub fn pa_checksum(data: &[u8]) -> u32 {
    hashlittle(data, HASH_SEED)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── hashlittle tests ──

    #[test]
    fn test_hashlittle_known_vector() {
        // Known test vector from crimson-desert-unpacker (lazorr)
        let h = hashlittle(b"rendererconfigurationmaterial.xml", 0x000C5EDE);
        assert_eq!(h, 0xaf3dcef3);
    }

    #[test]
    fn test_hashlittle_empty() {
        // Empty input with seed should still produce deterministic hash
        let h = hashlittle(b"", HASH_SEED);
        let h2 = hashlittle(b"", HASH_SEED);
        assert_eq!(h, h2); // deterministic
    }

    #[test]
    fn test_hashlittle_different_seeds() {
        let h1 = hashlittle(b"test", 0);
        let h2 = hashlittle(b"test", 1);
        assert_ne!(h1, h2); // different seeds → different hashes
    }

    // ── Tail length coverage: 1-12 bytes (exercises every tail branch) ──

    #[test]
    fn test_hashlittle_tail_1() {
        let h = hashlittle(b"a", 0);
        let h2 = hashlittle(b"a", 0);
        assert_eq!(h, h2);
    }

    #[test]
    fn test_hashlittle_tail_2() {
        let h = hashlittle(b"ab", 0);
        assert_ne!(h, hashlittle(b"ac", 0));
    }

    #[test]
    fn test_hashlittle_tail_3() {
        let h = hashlittle(b"abc", 0);
        assert_ne!(h, hashlittle(b"abd", 0));
    }

    #[test]
    fn test_hashlittle_tail_4() {
        let h = hashlittle(b"abcd", 0);
        assert_ne!(h, hashlittle(b"abce", 0));
    }

    #[test]
    fn test_hashlittle_tail_5() {
        let h = hashlittle(b"abcde", 0);
        assert_ne!(h, hashlittle(b"abcdf", 0));
    }

    #[test]
    fn test_hashlittle_tail_8() {
        let h = hashlittle(b"abcdefgh", 0);
        assert_ne!(h, hashlittle(b"abcdefgi", 0));
    }

    #[test]
    fn test_hashlittle_tail_9() {
        let h = hashlittle(b"abcdefghi", 0);
        assert_ne!(h, hashlittle(b"abcdefghj", 0));
    }

    #[test]
    fn test_hashlittle_tail_12() {
        let h = hashlittle(b"abcdefghijkl", 0);
        assert_ne!(h, hashlittle(b"abcdefghijkm", 0));
    }

    #[test]
    fn test_hashlittle_13_bytes() {
        // 13 bytes = 1 loop iteration + 1 tail byte
        let h = hashlittle(b"abcdefghijklm", 0);
        assert_ne!(h, hashlittle(b"abcdefghijkln", 0));
    }

    #[test]
    fn test_hashlittle_24_bytes() {
        // 24 bytes = 2 loop iterations + 0 tail
        let h = hashlittle(b"abcdefghijklmnopqrstuvwx", 0);
        assert_ne!(h, hashlittle(b"abcdefghijklmnopqrstuvwy", 0));
    }

    // ── pa_checksum tests ──

    #[test]
    fn test_pa_checksum_deterministic() {
        let h1 = pa_checksum(b"test data");
        let h2 = pa_checksum(b"test data");
        assert_eq!(h1, h2);
    }

    #[test]
    fn test_pa_checksum_equals_hashlittle_seed() {
        // PaChecksum is algebraically identical to hashlittle(data, HASH_SEED).
        // Verified against MrIkso/CrimsonDesertTools PaChecksum.cs and
        // canonical Bob Jenkins lookup3.c.
        let inputs: &[&[u8]] = &[
            b"rendererconfigurationmaterial.xml",
            b"test",
            b"abcdefghijklmnop",
            b"a",
            b"short",
            b"a longer string that exceeds twelve bytes easily",
        ];
        for input in inputs {
            assert_eq!(
                pa_checksum(input),
                hashlittle(input, HASH_SEED),
                "pa_checksum != hashlittle(HASH_SEED) for {:?}",
                std::str::from_utf8(input).unwrap_or("<bytes>")
            );
        }
    }

    #[test]
    fn test_pa_checksum_empty() {
        // pa_checksum is hashlittle(data, HASH_SEED) — empty input is non-zero
        let h = pa_checksum(b"");
        assert_eq!(h, hashlittle(b"", HASH_SEED));
        assert_ne!(h, 0);
    }

    #[test]
    fn test_pa_checksum_varies_with_input() {
        assert_ne!(pa_checksum(b"aaa"), pa_checksum(b"bbb"));
    }
}
