//! LZ4 block compression/decompression for PAZ archives.
//!
//! Thin wrappers around lz4_flex::block — no frame header, raw block format,
//! matching `lz4.block.compress(data, store_size=False)` in Python.

/// Compress data using LZ4 block format (no frame header).
pub fn compress(data: &[u8]) -> Vec<u8> {
    lz4_flex::block::compress(data)
}

/// Decompress LZ4 block data given the known uncompressed size.
pub fn decompress(data: &[u8], uncompressed_size: usize) -> Result<Vec<u8>, String> {
    lz4_flex::block::decompress(data, uncompressed_size)
        .map_err(|e| format!("LZ4 decompress failed: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_roundtrip() {
        let input = b"Hello people, what's up? This is a test of LZ4 block compression.";
        let compressed = compress(input);
        let decompressed = decompress(&compressed, input.len()).unwrap();
        assert_eq!(&decompressed, input);
    }

    #[test]
    fn test_empty() {
        let compressed = compress(b"");
        let decompressed = decompress(&compressed, 0).unwrap();
        assert!(decompressed.is_empty());
    }

    #[test]
    fn test_compresses_smaller() {
        // Repetitive data should compress well
        let input = vec![0xABu8; 1024];
        let compressed = compress(&input);
        assert!(compressed.len() < input.len());
    }

    #[test]
    fn test_wrong_uncompressed_size() {
        let input = b"test data for decompression";
        let compressed = compress(input);
        // Wrong size should fail or produce wrong output
        let result = decompress(&compressed, 5);
        // lz4_flex may error or produce truncated output
        assert!(result.is_err() || result.unwrap().len() != input.len());
    }

    #[test]
    fn test_large_data() {
        // Simulate a typical PAZ entry (~64KB)
        let input: Vec<u8> = (0..65536).map(|i| (i % 256) as u8).collect();
        let compressed = compress(&input);
        let decompressed = decompress(&compressed, input.len()).unwrap();
        assert_eq!(decompressed, input);
    }
}
