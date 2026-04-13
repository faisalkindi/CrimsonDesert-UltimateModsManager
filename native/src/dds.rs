//! DDS texture handling for PAZ archives.
//!
//! Compression type 0x01 in PAMT = DDS split format:
//! 128-byte raw DDS header + LZ4 compressed body (mip0).
//!
//! Exception: DX10 multi-mip textures are stored raw (no inner LZ4).

use crate::lz4_util;

const DDS_HEADER_SIZE: usize = 128;
const DDS_MAGIC: &[u8; 4] = b"DDS ";

/// Block compression sizes by FourCC.
fn bc_block_bytes(fourcc: &[u8; 4]) -> Option<u32> {
    match fourcc {
        b"DXT1" | b"ATI1" | b"BC4U" | b"BC4S" => Some(8),
        b"DXT2" | b"DXT3" | b"DXT4" | b"DXT5" | b"ATI2" | b"BC5U" | b"BC5S" => Some(16),
        _ => None,
    }
}

/// Block bytes for DXGI format codes.
fn bc_block_bytes_dxgi(dxgi: u32) -> Option<u32> {
    match dxgi {
        70 | 71 | 72 | 79 | 80 | 81 => Some(8),
        73..=78 | 82..=84 | 94..=99 => Some(16),
        _ => None,
    }
}

/// "last4" format identifier at DDS header byte 124.
fn dds_last4(fourcc: &[u8; 4], dxgi: Option<u32>) -> Option<u32> {
    let by_fourcc = match fourcc {
        b"DXT1" => Some(12),
        b"DXT2" | b"DXT3" | b"DXT4" | b"DXT5" => Some(15),
        b"ATI1" | b"ATI2" | b"BC4U" | b"BC4S" | b"BC5U" | b"BC5S" => Some(4),
        _ => None,
    };
    if by_fourcc.is_some() {
        return by_fourcc;
    }
    match dxgi? {
        71 | 72 => Some(12),       // BC1
        74 | 75 => Some(15),       // BC2
        77 | 78 => Some(15),       // BC3
        80 | 81 => Some(4),        // BC4
        83 | 84 => Some(4),        // BC5
        95 | 96 => Some(4),        // BC6H
        98 | 99 => Some(15),       // BC7
        _ => None,
    }
}

/// Check if a DDS file is DX10 multi-mip (raw passthrough, no inner LZ4).
pub fn is_dx10_multimip(data: &[u8]) -> bool {
    if data.len() < 148 {
        return false;
    }
    let fourcc = &data[84..88];
    if fourcc != b"DX10" {
        return false;
    }
    let mip_count = if data.len() >= 32 {
        u32::from_le_bytes([data[28], data[29], data[30], data[31]]).max(1)
    } else {
        1
    };
    mip_count > 1
}

/// Fix a DDS header to match game engine expectations.
///
/// Sets flag 0x20000, depth >= 1, mip chain sizes in reserved1,
/// and format-specific "last4" identifier at byte 124.
pub fn fix_dds_header(header: &mut [u8], compressed_body_size: u32) {
    if header.len() < DDS_HEADER_SIZE || &header[..4] != DDS_MAGIC {
        return;
    }

    // Fix flags: ensure 0x20000 is set
    let flags = u32::from_le_bytes([header[8], header[9], header[10], header[11]]);
    let flags = flags | 0x20000;
    header[8..12].copy_from_slice(&flags.to_le_bytes());

    // Fix depth: must be >= 1
    let depth = u32::from_le_bytes([header[24], header[25], header[26], header[27]]);
    if depth == 0 {
        header[24..28].copy_from_slice(&1u32.to_le_bytes());
    }

    // Read dimensions and format
    let height = u32::from_le_bytes([header[12], header[13], header[14], header[15]]);
    let width = u32::from_le_bytes([header[16], header[17], header[18], header[19]]);
    let mips = u32::from_le_bytes([header[28], header[29], header[30], header[31]]).max(1);
    let fourcc: [u8; 4] = [header[84], header[85], header[86], header[87]];

    let dxgi = if &fourcc == b"DX10" && header.len() >= 132 {
        Some(u32::from_le_bytes([header[128], header[129], header[130], header[131]]))
    } else {
        None
    };

    // Get block size
    let block_bytes = bc_block_bytes(&fourcc)
        .or_else(|| dxgi.and_then(bc_block_bytes_dxgi));

    if let Some(bb) = block_bytes {
        // Compute mip chain sizes
        let mut mip_sizes = [0u32; 4];
        let (mut cw, mut ch) = (width.max(1), height.max(1));
        for i in 0..4.min(mips as usize) {
            let bw = ((cw + 3) / 4).max(1);
            let bh = ((ch + 3) / 4).max(1);
            mip_sizes[i] = bw * bh * bb;
            cw = (cw / 2).max(1);
            ch = (ch / 2).max(1);
        }

        // Reserved1: [comp_body_size, decomp_mip0, mip1_size, mip2_size]
        header[32..36].copy_from_slice(&compressed_body_size.to_le_bytes());
        header[36..40].copy_from_slice(&mip_sizes[0].to_le_bytes());
        header[40..44].copy_from_slice(&mip_sizes[1].to_le_bytes());
        header[44..48].copy_from_slice(&mip_sizes[2].to_le_bytes());
        // Zero remaining reserved1 (offsets 48-75)
        for off in (48..76).step_by(4) {
            header[off..off + 4].copy_from_slice(&0u32.to_le_bytes());
        }
    }

    // Fix "last4" at byte 124
    if let Some(l4) = dds_last4(&fourcc, dxgi) {
        header[124..128].copy_from_slice(&l4.to_le_bytes());
    }
}

/// Compress a DDS file for overlay PAZ (type 0x01).
///
/// Returns (payload, comp_size, decomp_size).
/// - DX10 multi-mip: raw passthrough
/// - Standard DDS: 128-byte header (with fixed fields) + LZ4 compressed body,
///   padded to full decompressed size.
pub fn compress_dds(content: &[u8]) -> (Vec<u8>, u32, u32) {
    if is_dx10_multimip(content) {
        // Raw passthrough
        return (content.to_vec(), content.len() as u32, content.len() as u32);
    }

    if content.len() <= DDS_HEADER_SIZE {
        // Too small to split — store raw
        return (content.to_vec(), content.len() as u32, content.len() as u32);
    }

    let mut header = content[..DDS_HEADER_SIZE].to_vec();
    let body = &content[DDS_HEADER_SIZE..];

    let compressed_body = lz4_util::compress(body);

    if header[..4] == *DDS_MAGIC {
        fix_dds_header(&mut header, compressed_body.len() as u32);
    }

    let full_size = content.len();
    let mut payload = Vec::with_capacity(full_size);
    payload.extend_from_slice(&header);
    payload.extend_from_slice(&compressed_body);

    // Pad to full decompressed size
    if payload.len() < full_size {
        payload.resize(full_size, 0);
    }

    (payload, full_size as u32, full_size as u32)
}

/// Decompress a DDS type 0x01 entry from PAZ.
///
/// Returns the decompressed DDS file (header + body).
pub fn decompress_dds(raw: &[u8], orig_size: u32) -> Result<Vec<u8>, String> {
    if raw.len() < DDS_HEADER_SIZE {
        return Err("DDS data too small for header".into());
    }

    let header = &raw[..DDS_HEADER_SIZE];
    let compressed_body = &raw[DDS_HEADER_SIZE..];
    if (orig_size as usize) < DDS_HEADER_SIZE {
        return Err(format!(
            "DDS orig_size ({orig_size}) smaller than header ({DDS_HEADER_SIZE})"
        ));
    }
    let body_orig_size = orig_size as usize - DDS_HEADER_SIZE;

    // Check if inner LZ4 compressed size is stored at header offset 32
    let inner_comp_size = if header.len() >= 36 {
        u32::from_le_bytes([header[32], header[33], header[34], header[35]]) as usize
    } else {
        0
    };

    let lz4_input = if inner_comp_size > 0 && inner_comp_size < compressed_body.len() {
        &compressed_body[..inner_comp_size]
    } else {
        compressed_body
    };

    match lz4_util::decompress(lz4_input, body_orig_size) {
        Ok(body) => {
            let mut result = Vec::with_capacity(DDS_HEADER_SIZE + body.len());
            result.extend_from_slice(header);
            result.extend_from_slice(&body);
            Ok(result)
        }
        Err(_) if lz4_input.len() != compressed_body.len() => {
            // Retry with full body (vanilla entries without header field)
            let body = lz4_util::decompress(compressed_body, body_orig_size)?;
            let mut result = Vec::with_capacity(DDS_HEADER_SIZE + body.len());
            result.extend_from_slice(header);
            result.extend_from_slice(&body);
            Ok(result)
        }
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_dds_header(fourcc: &[u8; 4], width: u32, height: u32, mips: u32) -> Vec<u8> {
        let mut h = vec![0u8; 128];
        h[0..4].copy_from_slice(b"DDS ");
        h[8..12].copy_from_slice(&0u32.to_le_bytes()); // flags
        h[12..16].copy_from_slice(&height.to_le_bytes());
        h[16..20].copy_from_slice(&width.to_le_bytes());
        h[24..28].copy_from_slice(&0u32.to_le_bytes()); // depth = 0
        h[28..32].copy_from_slice(&mips.to_le_bytes());
        h[84..88].copy_from_slice(fourcc);
        h
    }

    #[test]
    fn test_fix_dds_header_flags_and_depth() {
        let mut h = make_dds_header(b"DXT1", 256, 256, 1);
        fix_dds_header(&mut h, 1000);
        let flags = u32::from_le_bytes([h[8], h[9], h[10], h[11]]);
        assert!(flags & 0x20000 != 0);
        let depth = u32::from_le_bytes([h[24], h[25], h[26], h[27]]);
        assert!(depth >= 1);
    }

    #[test]
    fn test_fix_dds_header_mip_sizes() {
        let mut h = make_dds_header(b"DXT1", 256, 256, 4);
        fix_dds_header(&mut h, 5000);
        let comp = u32::from_le_bytes([h[32], h[33], h[34], h[35]]);
        assert_eq!(comp, 5000);
        let mip0 = u32::from_le_bytes([h[36], h[37], h[38], h[39]]);
        // DXT1: 8 bytes per 4x4 block, 256x256 = 64*64 blocks = 4096 * 8 = 32768
        assert_eq!(mip0, 32768);
    }

    #[test]
    fn test_fix_dds_header_last4() {
        let mut h = make_dds_header(b"DXT5", 64, 64, 1);
        fix_dds_header(&mut h, 100);
        let last4 = u32::from_le_bytes([h[124], h[125], h[126], h[127]]);
        assert_eq!(last4, 15);
    }

    #[test]
    fn test_is_dx10_multimip() {
        let mut data = vec![0u8; 200];
        data[84..88].copy_from_slice(b"DX10");
        data[28..32].copy_from_slice(&3u32.to_le_bytes()); // 3 mips
        assert!(is_dx10_multimip(&data));

        // Single mip = not multimip
        data[28..32].copy_from_slice(&1u32.to_le_bytes());
        assert!(!is_dx10_multimip(&data));

        // Non-DX10
        data[84..88].copy_from_slice(b"DXT1");
        assert!(!is_dx10_multimip(&data));
    }

    #[test]
    fn test_compress_dds_roundtrip() {
        let header = make_dds_header(b"DXT1", 64, 64, 1);
        let body = vec![0xABu8; 2048]; // mip0 body
        let mut dds = Vec::new();
        dds.extend_from_slice(&header);
        dds.extend_from_slice(&body);

        let (payload, comp_size, decomp_size) = compress_dds(&dds);
        assert_eq!(comp_size, dds.len() as u32);
        assert_eq!(decomp_size, dds.len() as u32);

        // Header should be preserved (first 128 bytes after fix)
        assert_eq!(&payload[..4], b"DDS ");

        // Decompress should recover original body
        let recovered = decompress_dds(&payload, decomp_size).unwrap();
        assert_eq!(recovered[DDS_HEADER_SIZE..], body[..]);
    }

    #[test]
    fn test_compress_dds_dx10_multimip_passthrough() {
        let mut data = vec![0u8; 300];
        data[0..4].copy_from_slice(b"DDS ");
        data[84..88].copy_from_slice(b"DX10");
        data[28..32].copy_from_slice(&4u32.to_le_bytes()); // multi-mip
        let (payload, cs, ds) = compress_dds(&data);
        assert_eq!(payload, data); // raw passthrough
        assert_eq!(cs, ds);
    }
}
