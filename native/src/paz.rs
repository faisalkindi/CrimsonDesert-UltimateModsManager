//! PAMT index parser for Crimson Desert PAZ archives.
//!
//! Parses raw `.pamt` bytes to discover file entries, their locations in PAZ
//! archives, sizes, and compression info.  No file I/O — the caller provides
//! the bytes.
//!
//! Ported from `cdumm/archive/paz_parse.py`.

use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/// A single file entry in a PAZ archive.
#[derive(Debug, Clone)]
pub struct PazEntry {
    /// Full virtual path within the archive (e.g. `"data/config.xml"`).
    pub path: String,
    /// Which `.paz` file holds this entry (low byte of `flags`).
    pub paz_index: u32,
    /// Byte offset inside the `.paz` file.
    pub offset: u64,
    /// Compressed / stored size in the PAZ.
    pub comp_size: u32,
    /// Original decompressed size.
    pub orig_size: u32,
    /// Raw PAMT flags word.
    pub flags: u32,
}

impl PazEntry {
    /// Compression algorithm (0 = none, 2 = LZ4, 3 = custom, 4 = zlib).
    pub fn compression_type(&self) -> u32 {
        (self.flags >> 16) & 0x0F
    }

    /// Whether the stored data differs from the original (i.e. is compressed).
    pub fn is_compressed(&self) -> bool {
        self.comp_size != self.orig_size
    }

    /// Heuristic: entries whose path ends in `.xml`, `.css`, `.html`, or
    /// `.thtml` are ChaCha20-encrypted on disk.
    pub fn is_encrypted(&self) -> bool {
        let p = self.path.to_lowercase();
        p.ends_with(".xml")
            || p.ends_with(".css")
            || p.ends_with(".html")
            || p.ends_with(".thtml")
    }
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/// Errors returned when PAMT data is too short or structurally invalid.
#[derive(Debug)]
pub enum PamtError {
    /// The buffer was shorter than expected at the given context.
    UnexpectedEof { context: &'static str, offset: usize },
}

impl std::fmt::Display for PamtError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PamtError::UnexpectedEof { context, offset } => {
                write!(f, "PAMT unexpected EOF at offset {offset:#x} ({context})")
            }
        }
    }
}

impl std::error::Error for PamtError {}

// ---------------------------------------------------------------------------
// Little-endian read helpers
// ---------------------------------------------------------------------------

/// Read a `u32` (little-endian) at `offset`, or return an error.
fn read_u32(data: &[u8], offset: usize, ctx: &'static str) -> Result<u32, PamtError> {
    if offset + 4 > data.len() {
        return Err(PamtError::UnexpectedEof {
            context: ctx,
            offset,
        });
    }
    Ok(u32::from_le_bytes([
        data[offset],
        data[offset + 1],
        data[offset + 2],
        data[offset + 3],
    ]))
}

/// Read a single byte at `offset`, or return an error.
fn read_u8(data: &[u8], offset: usize, ctx: &'static str) -> Result<u8, PamtError> {
    if offset >= data.len() {
        return Err(PamtError::UnexpectedEof {
            context: ctx,
            offset,
        });
    }
    Ok(data[offset])
}

// ---------------------------------------------------------------------------
// Path reconstruction
// ---------------------------------------------------------------------------

/// Walk parent pointers to reconstruct a full path from the node trie.
///
/// `nodes` maps relative-offset -> (parent_offset, name_fragment).
/// Root entries have `parent == 0xFFFF_FFFF`.
fn build_path(nodes: &HashMap<usize, (u32, String)>, node_ref: u32) -> String {
    let mut parts: Vec<&str> = Vec::new();
    let mut cur = node_ref;

    // Safety: cap at 64 components to avoid infinite loops on corrupt data.
    for _ in 0..64 {
        if cur == 0xFFFF_FFFF {
            break;
        }
        match nodes.get(&(cur as usize)) {
            Some((parent, name)) => {
                parts.push(name.as_str());
                cur = *parent;
            }
            None => break,
        }
    }

    parts.reverse();
    parts.concat()
}

// ---------------------------------------------------------------------------
// Main parser
// ---------------------------------------------------------------------------

/// Parse raw PAMT bytes into a list of [`PazEntry`] values.
///
/// The function performs no file I/O — the caller reads the `.pamt` file and
/// passes its contents here.
pub fn parse_pamt(data: &[u8]) -> Result<Vec<PazEntry>, PamtError> {
    let mut off: usize = 0;

    // ── Header ────────────────────────────────────────────────────────
    // [0:4]  magic (varies, skip)
    // [4:8]  paz_count (u32 LE)
    // [8:16] hash + zero (skip 8 bytes)
    off += 4; // skip magic
    let paz_count = read_u32(data, off, "header paz_count")?;
    off += 4;
    off += 8; // hash + zero

    // ── PAZ table ─────────────────────────────────────────────────────
    // Each entry: hash(4) + size(4), with separator(4) between entries.
    for i in 0..paz_count {
        off += 4; // hash
        off += 4; // size
        if i < paz_count - 1 {
            off += 4; // separator (absent after last entry)
        }
    }

    // ── Folder section ────────────────────────────────────────────────
    // folder_size(4) then variable-length entries until folder_end.
    let folder_size = read_u32(data, off, "folder_size")? as usize;
    off += 4;
    let folder_end = off + folder_size;

    let mut folder_prefix = String::new();
    while off < folder_end {
        let parent = read_u32(data, off, "folder parent")?;
        let slen = read_u8(data, off + 4, "folder name_len")? as usize;
        if off + 5 + slen > data.len() {
            return Err(PamtError::UnexpectedEof {
                context: "folder name bytes",
                offset: off + 5,
            });
        }
        let name = String::from_utf8_lossy(&data[off + 5..off + 5 + slen]).into_owned();
        if parent == 0xFFFF_FFFF {
            folder_prefix = name;
        }
        off += 5 + slen;
    }

    // ── Node section (filename trie) ──────────────────────────────────
    let node_size = read_u32(data, off, "node_size")? as usize;
    off += 4;
    let node_start = off;
    let mut nodes: HashMap<usize, (u32, String)> = HashMap::new();

    while off < node_start + node_size {
        let rel = off - node_start;
        let parent = read_u32(data, off, "node parent")?;
        let slen = read_u8(data, off + 4, "node name_len")? as usize;
        if off + 5 + slen > data.len() {
            return Err(PamtError::UnexpectedEof {
                context: "node name bytes",
                offset: off + 5,
            });
        }
        let name = String::from_utf8_lossy(&data[off + 5..off + 5 + slen]).into_owned();
        nodes.insert(rel, (parent, name));
        off += 5 + slen;
    }

    // ── Folder record section ─────────────────────────────────────────
    // folder_count(4) + records (16 bytes each).
    let folder_count = read_u32(data, off, "folder_count")? as usize;
    off += 4;
    off += folder_count * 16;

    // ── File record section ───────────────────────────────────────────
    // file_count(4) + records (20 bytes each):
    //   node_ref(4) + paz_offset(4) + comp_size(4) + orig_size(4) + flags(4)
    let file_count = read_u32(data, off, "file_count")? as usize;
    off += 4;

    let mut entries: Vec<PazEntry> = Vec::with_capacity(file_count.min(10_000));

    for _ in 0..file_count {
        if off + 20 > data.len() {
            break;
        }
        let node_ref = read_u32(data, off, "file node_ref")?;
        let paz_offset = read_u32(data, off + 4, "file paz_offset")?;
        let comp_size = read_u32(data, off + 8, "file comp_size")?;
        let orig_size = read_u32(data, off + 12, "file orig_size")?;
        let flags = read_u32(data, off + 16, "file flags")?;
        off += 20;

        let paz_index = flags & 0xFF;

        let node_path = build_path(&nodes, node_ref);
        let full_path = if folder_prefix.is_empty() {
            node_path
        } else {
            format!("{}/{}", folder_prefix, node_path)
        };

        entries.push(PazEntry {
            path: full_path,
            paz_index,
            offset: paz_offset as u64,
            comp_size,
            orig_size,
            flags,
        });
    }

    Ok(entries)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper: write a u32 LE into a byte vec.
    fn push_u32(buf: &mut Vec<u8>, val: u32) {
        buf.extend_from_slice(&val.to_le_bytes());
    }

    /// Helper: write a name entry (parent_offset(4) + name_len(1) + name(N)).
    fn push_name_entry(buf: &mut Vec<u8>, parent: u32, name: &str) {
        push_u32(buf, parent);
        buf.push(name.len() as u8);
        buf.extend_from_slice(name.as_bytes());
    }

    /// Build a minimal but valid PAMT buffer with the given entries.
    ///
    /// Creates one PAZ table entry, one root folder "data", a set of node
    /// entries to form the filename trie, one folder record, and the
    /// requested file records.
    struct PamtBuilder {
        folder_name: String,
        /// (parent_offset_within_node_section, name_fragment)
        node_entries: Vec<(u32, String)>,
        /// (node_ref, paz_offset, comp_size, orig_size, flags)
        file_records: Vec<(u32, u32, u32, u32, u32)>,
    }

    impl PamtBuilder {
        fn new(folder: &str) -> Self {
            Self {
                folder_name: folder.to_string(),
                node_entries: Vec::new(),
                file_records: Vec::new(),
            }
        }

        /// Push a node entry and return its relative offset (for referencing
        /// as a parent or from a file record).
        fn add_node(&mut self, parent: u32, name: &str) -> u32 {
            // Each prior entry takes 4 (parent) + 1 (len) + N (name) bytes.
            let rel: usize = self
                .node_entries
                .iter()
                .map(|(_, n)| 4 + 1 + n.len())
                .sum();
            self.node_entries.push((parent, name.to_string()));
            rel as u32
        }

        fn add_file(&mut self, node_ref: u32, offset: u32, comp: u32, orig: u32, flags: u32) {
            self.file_records.push((node_ref, offset, comp, orig, flags));
        }

        fn build(&self) -> Vec<u8> {
            let mut buf = Vec::new();

            // Header
            push_u32(&mut buf, 0xDEADBEEF); // magic
            push_u32(&mut buf, 1);           // paz_count = 1
            push_u32(&mut buf, 0);           // hash
            push_u32(&mut buf, 0);           // zero

            // PAZ table (1 entry, no separator after last)
            push_u32(&mut buf, 0xAAAAAAAA); // hash
            push_u32(&mut buf, 0x1000);     // size

            // Folder section
            let mut folder_buf = Vec::new();
            push_name_entry(&mut folder_buf, 0xFFFF_FFFF, &self.folder_name);
            push_u32(&mut buf, folder_buf.len() as u32);
            buf.extend_from_slice(&folder_buf);

            // Node section
            let mut node_buf = Vec::new();
            for (parent, name) in &self.node_entries {
                push_name_entry(&mut node_buf, *parent, name);
            }
            push_u32(&mut buf, node_buf.len() as u32);
            buf.extend_from_slice(&node_buf);

            // Folder record section (1 dummy record)
            push_u32(&mut buf, 1); // folder_count
            buf.extend_from_slice(&[0u8; 16]); // one 16-byte record

            // File record section
            push_u32(&mut buf, self.file_records.len() as u32);
            for &(node_ref, offset, comp, orig, flags) in &self.file_records {
                push_u32(&mut buf, node_ref);
                push_u32(&mut buf, offset);
                push_u32(&mut buf, comp);
                push_u32(&mut buf, orig);
                push_u32(&mut buf, flags);
            }

            buf
        }
    }

    // ----- build_path tests -----------------------------------------------

    #[test]
    fn build_path_single_root_node() {
        let mut nodes = HashMap::new();
        nodes.insert(0usize, (0xFFFF_FFFF, "readme.txt".to_string()));
        assert_eq!(build_path(&nodes, 0), "readme.txt");
    }

    #[test]
    fn build_path_chain() {
        // 0 -> root "textures/"
        // 9 -> parent 0, "skin/"
        // 18 -> parent 9, "head.dds"
        let mut nodes = HashMap::new();
        nodes.insert(0, (0xFFFF_FFFF, "textures/".to_string()));
        // "textures/" = 4+1+9 = 14 bytes, but we control offsets manually.
        nodes.insert(14, (0, "skin/".to_string()));
        nodes.insert(23, (14, "head.dds".to_string()));
        assert_eq!(build_path(&nodes, 23), "textures/skin/head.dds");
    }

    #[test]
    fn build_path_missing_ref() {
        let nodes: HashMap<usize, (u32, String)> = HashMap::new();
        // Referencing a node that does not exist gives an empty path.
        assert_eq!(build_path(&nodes, 42), "");
    }

    #[test]
    fn build_path_depth_limit() {
        // Create a chain longer than 64 to verify the safety cap.
        let mut nodes = HashMap::new();
        for i in 0..100usize {
            let parent = if i == 0 { 0xFFFF_FFFF } else { (i - 1) as u32 };
            nodes.insert(i, (parent, "x".to_string()));
        }
        let path = build_path(&nodes, 99);
        // Should be capped at 64 components.
        assert_eq!(path.len(), 64);
    }

    // ----- PazEntry method tests ------------------------------------------

    #[test]
    fn compression_type_extraction() {
        let e = PazEntry {
            path: "test.bin".into(),
            paz_index: 0,
            offset: 0,
            comp_size: 100,
            orig_size: 200,
            flags: 0x0002_0000, // compression_type = 2 (LZ4)
        };
        assert_eq!(e.compression_type(), 2);
    }

    #[test]
    fn is_compressed_true_when_sizes_differ() {
        let e = PazEntry {
            path: "a.bin".into(),
            paz_index: 0,
            offset: 0,
            comp_size: 50,
            orig_size: 100,
            flags: 0,
        };
        assert!(e.is_compressed());
    }

    #[test]
    fn is_compressed_false_when_sizes_equal() {
        let e = PazEntry {
            path: "a.bin".into(),
            paz_index: 0,
            offset: 0,
            comp_size: 100,
            orig_size: 100,
            flags: 0,
        };
        assert!(!e.is_compressed());
    }

    #[test]
    fn is_encrypted_for_known_extensions() {
        for ext in &[".xml", ".XML", ".Xml", ".css", ".html", ".thtml"] {
            let e = PazEntry {
                path: format!("data/file{ext}"),
                paz_index: 0,
                offset: 0,
                comp_size: 0,
                orig_size: 0,
                flags: 0,
            };
            assert!(e.is_encrypted(), "expected encrypted for {ext}");
        }
    }

    #[test]
    fn is_not_encrypted_for_other_extensions() {
        for ext in &[".dds", ".bin", ".pac", ".paac"] {
            let e = PazEntry {
                path: format!("data/file{ext}"),
                paz_index: 0,
                offset: 0,
                comp_size: 0,
                orig_size: 0,
                flags: 0,
            };
            assert!(!e.is_encrypted(), "expected NOT encrypted for {ext}");
        }
    }

    // ----- parse_pamt tests -----------------------------------------------

    #[test]
    fn parse_minimal_pamt() {
        let mut b = PamtBuilder::new("data");
        let n = b.add_node(0xFFFF_FFFF, "test.bin");
        b.add_file(n, 0x100, 50, 100, 0x0002_0000);

        let entries = parse_pamt(&b.build()).unwrap();
        assert_eq!(entries.len(), 1);

        let e = &entries[0];
        assert_eq!(e.path, "data/test.bin");
        assert_eq!(e.paz_index, 0);
        assert_eq!(e.offset, 0x100);
        assert_eq!(e.comp_size, 50);
        assert_eq!(e.orig_size, 100);
        assert_eq!(e.compression_type(), 2);
        assert!(e.is_compressed());
    }

    #[test]
    fn parse_multiple_files() {
        let mut b = PamtBuilder::new("art");
        let dir = b.add_node(0xFFFF_FFFF, "tex/");
        let n1 = b.add_node(dir, "a.dds");
        let n2 = b.add_node(dir, "b.dds");
        b.add_file(n1, 0, 1000, 1000, 0x0000_0000);
        b.add_file(n2, 1000, 500, 800, 0x0002_0001);

        let entries = parse_pamt(&b.build()).unwrap();
        assert_eq!(entries.len(), 2);

        assert_eq!(entries[0].path, "art/tex/a.dds");
        assert!(!entries[0].is_compressed());
        assert_eq!(entries[0].paz_index, 0);

        assert_eq!(entries[1].path, "art/tex/b.dds");
        assert!(entries[1].is_compressed());
        assert_eq!(entries[1].paz_index, 1);
    }

    #[test]
    fn parse_empty_folder_prefix() {
        // Folder section with an empty name on the root entry.
        let mut buf = Vec::new();

        // Header
        push_u32(&mut buf, 0); // magic
        push_u32(&mut buf, 1); // paz_count
        push_u32(&mut buf, 0); // hash
        push_u32(&mut buf, 0); // zero

        // PAZ table (1 entry)
        push_u32(&mut buf, 0);
        push_u32(&mut buf, 0);

        // Folder section: root entry with empty name
        let mut folder_buf = Vec::new();
        push_name_entry(&mut folder_buf, 0xFFFF_FFFF, "");
        push_u32(&mut buf, folder_buf.len() as u32);
        buf.extend_from_slice(&folder_buf);

        // Node section: one node
        let mut node_buf = Vec::new();
        push_name_entry(&mut node_buf, 0xFFFF_FFFF, "plain.txt");
        push_u32(&mut buf, node_buf.len() as u32);
        buf.extend_from_slice(&node_buf);

        // Folder records: 0
        push_u32(&mut buf, 0);

        // File records
        push_u32(&mut buf, 1);
        push_u32(&mut buf, 0);    // node_ref
        push_u32(&mut buf, 0);    // offset
        push_u32(&mut buf, 10);   // comp_size
        push_u32(&mut buf, 10);   // orig_size
        push_u32(&mut buf, 0);    // flags

        let entries = parse_pamt(&buf).unwrap();
        assert_eq!(entries.len(), 1);
        // No folder prefix -> path is just the node path.
        assert_eq!(entries[0].path, "plain.txt");
    }

    #[test]
    fn parse_paz_index_from_flags() {
        let mut b = PamtBuilder::new("d");
        let n = b.add_node(0xFFFF_FFFF, "f.bin");
        // paz_index = 3 (low byte), compression_type = 4 (bits 16-19)
        b.add_file(n, 0, 10, 20, 0x0004_0003);

        let entries = parse_pamt(&b.build()).unwrap();
        assert_eq!(entries[0].paz_index, 3);
        assert_eq!(entries[0].compression_type(), 4);
    }

    #[test]
    fn parse_multiple_paz_table_entries() {
        // 3 PAZ table entries = separators between first two pairs.
        let mut buf = Vec::new();

        // Header
        push_u32(&mut buf, 0xCAFE);
        push_u32(&mut buf, 3); // paz_count
        push_u32(&mut buf, 0);
        push_u32(&mut buf, 0);

        // PAZ table: 3 entries with separators between them
        for i in 0u32..3 {
            push_u32(&mut buf, i);    // hash
            push_u32(&mut buf, 0x100); // size
            if i < 2 {
                push_u32(&mut buf, 0); // separator
            }
        }

        // Empty folder section
        let mut folder_buf = Vec::new();
        push_name_entry(&mut folder_buf, 0xFFFF_FFFF, "root");
        push_u32(&mut buf, folder_buf.len() as u32);
        buf.extend_from_slice(&folder_buf);

        // One node
        let mut node_buf = Vec::new();
        push_name_entry(&mut node_buf, 0xFFFF_FFFF, "x.dat");
        push_u32(&mut buf, node_buf.len() as u32);
        buf.extend_from_slice(&node_buf);

        // 0 folder records
        push_u32(&mut buf, 0);

        // 1 file record
        push_u32(&mut buf, 1);
        push_u32(&mut buf, 0);
        push_u32(&mut buf, 0);
        push_u32(&mut buf, 5);
        push_u32(&mut buf, 5);
        push_u32(&mut buf, 0);

        let entries = parse_pamt(&buf).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].path, "root/x.dat");
    }

    #[test]
    fn parse_truncated_header_returns_error() {
        let buf = vec![0u8; 6]; // too short for header
        assert!(parse_pamt(&buf).is_err());
    }

    #[test]
    fn parse_zero_files() {
        let b = PamtBuilder::new("empty");
        // No nodes, no file records added.
        let entries = parse_pamt(&b.build()).unwrap();
        assert!(entries.is_empty());
    }

    #[test]
    fn encrypted_xml_in_subfolder() {
        let mut b = PamtBuilder::new("config");
        let dir = b.add_node(0xFFFF_FFFF, "ui/");
        let n = b.add_node(dir, "settings.xml");
        b.add_file(n, 0, 30, 30, 0);

        let entries = parse_pamt(&b.build()).unwrap();
        assert!(entries[0].is_encrypted());
    }

    #[test]
    fn offset_stored_as_u64() {
        let mut b = PamtBuilder::new("d");
        let n = b.add_node(0xFFFF_FFFF, "big.bin");
        // Large offset that fits in u32 but is stored as u64 in PazEntry.
        b.add_file(n, 0xFFFF_FFF0, 1, 1, 0);

        let entries = parse_pamt(&b.build()).unwrap();
        assert_eq!(entries[0].offset, 0xFFFF_FFF0u64);
    }
}
