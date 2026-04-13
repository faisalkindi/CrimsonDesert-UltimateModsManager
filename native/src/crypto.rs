//! ChaCha20 encryption/decryption with deterministic key derivation
//! for Crimson Desert PAZ archives.
//!
//! Keys are derived from the filename alone — no key database needed.
//! The derivation uses hashlittle (Bob Jenkins lookup3) seeded with
//! `HASH_SEED` to produce a 32-byte key and 16-byte IV from the
//! lowercased basename.
//!
//! Encrypted file extensions: `.xml`, `.css`, `.html`, `.thtml`

use chacha20::cipher::{KeyIvInit, StreamCipher, StreamCipherSeek};
use chacha20::ChaCha20;

use crate::hashlittle::{hashlittle, HASH_SEED};

// ── Key derivation constants ────────────────────────────────────────

/// XOR mask applied to the seed to produce the key base word.
const IV_XOR: u32 = 0x6061_6263;

/// Per-word XOR deltas applied to key_base to produce 8 key words (32 bytes).
const XOR_DELTAS: [u32; 8] = [
    0x0000_0000,
    0x0A0A_0A0A,
    0x0C0C_0C0C,
    0x0606_0606,
    0x0E0E_0E0E,
    0x0A0A_0A0A,
    0x0606_0606,
    0x0202_0202,
];

/// File extensions that the game encrypts with ChaCha20.
pub const ENCRYPTED_EXTENSIONS: &[&str] = &[".xml", ".css", ".html", ".thtml"];

// ── Key derivation ──────────────────────────────────────────────────

/// Derive a 32-byte ChaCha20 key and 16-byte IV from a PAZ filename.
///
/// The filename is lowercased and only the basename (after the last
/// path separator) is used. The hash seed is the standard `HASH_SEED`
/// constant shared with PAMT/PAPGT integrity checks.
///
/// Returns `(key, iv)` where key is 32 bytes and iv is 16 bytes.
pub fn derive_key_iv(filename: &str) -> ([u8; 32], [u8; 16]) {
    // Extract basename: find the last occurrence of either separator
    let last_fwd = filename.rfind('/');
    let last_bck = filename.rfind('\\');
    let last_sep = match (last_fwd, last_bck) {
        (Some(a), Some(b)) => Some(a.max(b)),
        (Some(a), None) => Some(a),
        (None, Some(b)) => Some(b),
        (None, None) => None,
    };
    let basename = match last_sep {
        Some(pos) => &filename[pos + 1..],
        None => filename,
    };
    let lower = basename.to_ascii_lowercase();

    // Hash the lowercased basename
    let seed = hashlittle(lower.as_bytes(), HASH_SEED);

    // IV: seed repeated 4 times as little-endian u32 → 16 bytes
    let seed_le = seed.to_le_bytes();
    let mut iv = [0u8; 16];
    iv[0..4].copy_from_slice(&seed_le);
    iv[4..8].copy_from_slice(&seed_le);
    iv[8..12].copy_from_slice(&seed_le);
    iv[12..16].copy_from_slice(&seed_le);

    // Key: (seed ^ IV_XOR) XORed with each delta → 8 words → 32 bytes
    let key_base = seed ^ IV_XOR;
    let mut key = [0u8; 32];
    for (i, delta) in XOR_DELTAS.iter().enumerate() {
        let word = key_base ^ delta;
        key[i * 4..i * 4 + 4].copy_from_slice(&word.to_le_bytes());
    }

    (key, iv)
}

// ── ChaCha20 encrypt/decrypt ────────────────────────────────────────

/// Apply ChaCha20 keystream to `data` in-place (symmetric: encrypt = decrypt).
///
/// The 16-byte IV is split for the IETF ChaCha20 interface:
/// - `iv[0..4]`  → initial counter (little-endian u32)
/// - `iv[4..16]` → 12-byte nonce
///
/// This matches the Python `cryptography` library's ChaCha20 which
/// accepts a 16-byte "nonce" where the first 4 bytes are the counter.
fn chacha20_apply(data: &mut [u8], key: &[u8; 32], iv: &[u8; 16]) {
    // Split IV: first 4 bytes = counter, remaining 12 = nonce
    let counter = u32::from_le_bytes([iv[0], iv[1], iv[2], iv[3]]);
    let nonce: &[u8; 12] = iv[4..16].try_into().expect("nonce is 12 bytes");

    let mut cipher = ChaCha20::new(key.into(), nonce.into());
    // Python's `cryptography` library treats the first 4 IV bytes as the
    // initial block counter.  The Rust `StreamCipherSeek::seek` trait takes
    // a *byte* offset.  Each ChaCha20 block is 64 bytes, so multiply.
    cipher.seek((counter as u64) * 64);
    cipher.apply_keystream(data);
}

/// Decrypt data in-place using a key derived from the filename.
pub fn decrypt_in_place(data: &mut [u8], filename: &str) {
    let (key, iv) = derive_key_iv(filename);
    chacha20_apply(data, &key, &iv);
}

/// Encrypt data in-place using a key derived from the filename.
/// (ChaCha20 is symmetric — this is identical to decrypt.)
pub fn encrypt_in_place(data: &mut [u8], filename: &str) {
    decrypt_in_place(data, filename);
}

/// Decrypt data, returning a new buffer.
pub fn decrypt(data: &[u8], filename: &str) -> Vec<u8> {
    let mut buf = data.to_vec();
    decrypt_in_place(&mut buf, filename);
    buf
}

/// Encrypt data, returning a new buffer.
pub fn encrypt(data: &[u8], filename: &str) -> Vec<u8> {
    decrypt(data, filename)
}

/// Returns true if the given filename has an extension that the game
/// encrypts with ChaCha20.
pub fn is_encrypted_extension(filename: &str) -> bool {
    let lower = filename.to_ascii_lowercase();
    ENCRYPTED_EXTENSIONS
        .iter()
        .any(|ext| lower.ends_with(ext))
}

// ── Tests ───────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── derive_key_iv tests ──

    #[test]
    fn test_derive_seed_known_vector() {
        // From the plan: rendererconfigurationmaterial.xml → seed 0xaf3dcef3
        let seed = hashlittle(b"rendererconfigurationmaterial.xml", HASH_SEED);
        assert_eq!(seed, 0xaf3dcef3);
    }

    #[test]
    fn test_derive_key_known_vector() {
        // Filename: rendererconfigurationmaterial.xml
        // Expected key (hex): 90ac5ccf9aa656c59ca050c396aa5ac99ea252c19aa656c596aa5ac992ae5ecd
        let (key, _iv) = derive_key_iv("rendererconfigurationmaterial.xml");
        let expected_key = hex_to_bytes(
            "90ac5ccf9aa656c59ca050c396aa5ac99ea252c19aa656c596aa5ac992ae5ecd",
        );
        assert_eq!(key.as_slice(), expected_key.as_slice());
    }

    #[test]
    fn test_derive_iv_known_vector() {
        // seed = 0xaf3dcef3, IV = seed repeated 4 times as LE u32
        let (_, iv) = derive_key_iv("rendererconfigurationmaterial.xml");
        let seed_le = 0xaf3dcef3_u32.to_le_bytes();
        let mut expected = [0u8; 16];
        expected[0..4].copy_from_slice(&seed_le);
        expected[4..8].copy_from_slice(&seed_le);
        expected[8..12].copy_from_slice(&seed_le);
        expected[12..16].copy_from_slice(&seed_le);
        assert_eq!(iv, expected);
    }

    #[test]
    fn test_derive_key_iv_manual_computation() {
        // Manually verify the key derivation math for the known vector.
        // seed = 0xaf3dcef3
        // key_base = seed ^ IV_XOR = 0xaf3dcef3 ^ 0x60616263 = 0xcf5cac90
        // word[0] = key_base ^ 0x00000000 = 0xcf5cac90
        // word[1] = key_base ^ 0x0A0A0A0A = 0xc556a69a
        // ...etc
        let seed: u32 = 0xaf3dcef3;
        let key_base = seed ^ IV_XOR;
        assert_eq!(key_base, 0xcf5cac90);

        let expected_words: Vec<u32> = XOR_DELTAS
            .iter()
            .map(|d| key_base ^ d)
            .collect();

        // Verify first and last words
        assert_eq!(expected_words[0], 0xcf5cac90);
        assert_eq!(expected_words[7], 0xcd5eae92);

        // Now verify they match the key bytes (little-endian)
        let (key, _) = derive_key_iv("rendererconfigurationmaterial.xml");
        for (i, word) in expected_words.iter().enumerate() {
            let got = u32::from_le_bytes([
                key[i * 4],
                key[i * 4 + 1],
                key[i * 4 + 2],
                key[i * 4 + 3],
            ]);
            assert_eq!(got, *word, "key word {} mismatch", i);
        }
    }

    // ── Basename and case handling ──

    #[test]
    fn test_derive_strips_path_forward_slash() {
        let (k1, iv1) = derive_key_iv("rendererconfigurationmaterial.xml");
        let (k2, iv2) = derive_key_iv("some/path/rendererconfigurationmaterial.xml");
        assert_eq!(k1, k2);
        assert_eq!(iv1, iv2);
    }

    #[test]
    fn test_derive_strips_path_backslash() {
        let (k1, iv1) = derive_key_iv("rendererconfigurationmaterial.xml");
        let (k2, iv2) = derive_key_iv("some\\path\\rendererconfigurationmaterial.xml");
        assert_eq!(k1, k2);
        assert_eq!(iv1, iv2);
    }

    #[test]
    fn test_derive_case_insensitive() {
        let (k1, iv1) = derive_key_iv("RENDERERCONFIGURATIONMATERIAL.XML");
        let (k2, iv2) = derive_key_iv("rendererconfigurationmaterial.xml");
        assert_eq!(k1, k2);
        assert_eq!(iv1, iv2);
    }

    #[test]
    fn test_derive_mixed_case() {
        let (k1, _) = derive_key_iv("RendererConfigurationMaterial.Xml");
        let (k2, _) = derive_key_iv("rendererconfigurationmaterial.xml");
        assert_eq!(k1, k2);
    }

    // ── ChaCha20 round-trip ──

    #[test]
    fn test_encrypt_decrypt_roundtrip() {
        let plaintext = b"Hello, Crimson Desert PAZ!";
        let filename = "test_file.xml";

        let ciphertext = encrypt(plaintext, filename);
        assert_ne!(ciphertext, plaintext.to_vec(), "ciphertext should differ from plaintext");

        let decrypted = decrypt(&ciphertext, filename);
        assert_eq!(decrypted, plaintext.to_vec());
    }

    #[test]
    fn test_encrypt_decrypt_roundtrip_in_place() {
        let plaintext = b"In-place round-trip test data for PAZ archives.";
        let filename = "data.xml";

        let mut buf = plaintext.to_vec();
        encrypt_in_place(&mut buf, filename);
        assert_ne!(buf, plaintext.to_vec());

        decrypt_in_place(&mut buf, filename);
        assert_eq!(buf, plaintext.to_vec());
    }

    #[test]
    fn test_encrypt_equals_decrypt() {
        // ChaCha20 is symmetric: encrypt and decrypt produce the same output
        let data = b"symmetric test";
        let filename = "sym.xml";

        let enc = encrypt(data, filename);
        let dec = decrypt(data, filename);
        assert_eq!(enc, dec);
    }

    #[test]
    fn test_empty_data() {
        let data: &[u8] = b"";
        let filename = "empty.xml";
        let enc = encrypt(data, filename);
        assert_eq!(enc, Vec::<u8>::new());
    }

    #[test]
    fn test_large_data() {
        // Test with data larger than one ChaCha20 block (64 bytes)
        let plaintext: Vec<u8> = (0..256).map(|i| (i & 0xFF) as u8).collect();
        let filename = "large_test.xml";

        let ciphertext = encrypt(&plaintext, filename);
        assert_eq!(ciphertext.len(), 256);
        assert_ne!(ciphertext, plaintext);

        let roundtrip = decrypt(&ciphertext, filename);
        assert_eq!(roundtrip, plaintext);
    }

    #[test]
    fn test_different_filenames_different_ciphertext() {
        let data = b"same data, different keys";
        let ct1 = encrypt(data, "file_a.xml");
        let ct2 = encrypt(data, "file_b.xml");
        assert_ne!(ct1, ct2, "different filenames should produce different ciphertext");
    }

    #[test]
    fn test_wrong_filename_does_not_decrypt() {
        let plaintext = b"secret data";
        let ciphertext = encrypt(plaintext, "correct.xml");
        let bad_decrypt = decrypt(&ciphertext, "wrong.xml");
        assert_ne!(bad_decrypt, plaintext.to_vec());
    }

    // ── is_encrypted_extension ──

    #[test]
    fn test_is_encrypted_xml() {
        assert!(is_encrypted_extension("data.xml"));
        assert!(is_encrypted_extension("path/to/file.XML"));
        assert!(is_encrypted_extension("CONFIG.Xml"));
    }

    #[test]
    fn test_is_encrypted_css() {
        assert!(is_encrypted_extension("style.css"));
        assert!(is_encrypted_extension("STYLE.CSS"));
    }

    #[test]
    fn test_is_encrypted_html() {
        assert!(is_encrypted_extension("page.html"));
        assert!(is_encrypted_extension("PAGE.HTML"));
    }

    #[test]
    fn test_is_encrypted_thtml() {
        assert!(is_encrypted_extension("template.thtml"));
        assert!(is_encrypted_extension("TEMPLATE.THTML"));
    }

    #[test]
    fn test_not_encrypted() {
        assert!(!is_encrypted_extension("image.png"));
        assert!(!is_encrypted_extension("model.pac"));
        assert!(!is_encrypted_extension("archive.paz"));
        assert!(!is_encrypted_extension("data.bin"));
        assert!(!is_encrypted_extension("noext"));
    }

    // ── Helper ──

    /// Convert a hex string to bytes (test utility).
    fn hex_to_bytes(hex: &str) -> Vec<u8> {
        (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect()
    }
}
