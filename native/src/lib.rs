//! cdumm_native — Rust native overlay engine for CDUMM.
//!
//! Handles the performance-critical PAZ I/O, LZ4 compression,
//! ChaCha20 encryption, pattern scan, and overlay building.
//! Called from Python via PyO3.

mod hashlittle;
mod crypto;
mod patcher;
pub mod paz;
mod lz4_util;
mod dds;
mod overlay;

use pyo3::prelude::*;
use pyo3::types::PyDict;

// ── Hash functions ──────────────────────────────────────────────────

#[pyfunction]
fn compute_hashlittle(data: &[u8], initval: u32) -> u32 {
    hashlittle::hashlittle(data, initval)
}

#[pyfunction]
fn compute_pa_checksum(data: &[u8]) -> u32 {
    hashlittle::pa_checksum(data)
}

// ── LZ4 compression ────────────────────────────────────────────────

#[pyfunction]
fn lz4_compress(py: Python<'_>, data: &[u8]) -> PyResult<PyObject> {
    let compressed = py.allow_threads(|| lz4_util::compress(data));
    Ok(pyo3::types::PyBytes::new(py, &compressed).into())
}

#[pyfunction]
fn lz4_decompress(py: Python<'_>, data: &[u8], uncompressed_size: usize) -> PyResult<PyObject> {
    let result = py.allow_threads(|| lz4_util::decompress(data, uncompressed_size));
    match result {
        Ok(decompressed) => Ok(pyo3::types::PyBytes::new(py, &decompressed).into()),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ── ChaCha20 encryption ────────────────────────────────────────────

#[pyfunction]
fn chacha20_decrypt(py: Python<'_>, data: &[u8], filename: &str) -> PyResult<PyObject> {
    let mut buf = data.to_vec();
    let fname = filename.to_string();
    py.allow_threads(|| crypto::decrypt_in_place(&mut buf, &fname));
    Ok(pyo3::types::PyBytes::new(py, &buf).into())
}

#[pyfunction]
fn chacha20_encrypt(py: Python<'_>, data: &[u8], filename: &str) -> PyResult<PyObject> {
    // ChaCha20 is symmetric — encrypt = decrypt
    chacha20_decrypt(py, data, filename)
}

#[pyfunction]
fn derive_key_iv(py: Python<'_>, filename: &str) -> PyResult<(PyObject, PyObject)> {
    let (key, iv) = crypto::derive_key_iv(filename);
    Ok((
        pyo3::types::PyBytes::new(py, &key).into(),
        pyo3::types::PyBytes::new(py, &iv).into(),
    ))
}

#[pyfunction]
fn is_encrypted_extension(filename: &str) -> bool {
    crypto::is_encrypted_extension(filename)
}

// ── Pattern scan ────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(signature = (data, offset, original, vanilla_data=None))]
fn pattern_scan(
    data: &[u8],
    offset: usize,
    original: &[u8],
    vanilla_data: Option<&[u8]>,
) -> Option<usize> {
    patcher::pattern_scan(data, offset, original, vanilla_data)
}

// ── Byte patching ───────────────────────────────────────────────────

/// Input dict: {"offset": int, "original": bytes|str, "patched": bytes|str, "type": "replace"|"insert"}
/// Accepts both raw bytes and hex strings for original/patched fields.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct ByteChangePy {
    offset: usize,
    original: HexOrBytes,
    patched: HexOrBytes,
    #[pyo3(item("type"))]
    change_type: String,
}

/// Accepts either Python bytes or a hex string, converting to Vec<u8>.
struct HexOrBytes(Vec<u8>);

impl<'py> pyo3::FromPyObject<'py> for HexOrBytes {
    fn extract_bound(ob: &pyo3::Bound<'py, pyo3::PyAny>) -> PyResult<Self> {
        // Try bytes first
        if let Ok(b) = ob.extract::<Vec<u8>>() {
            return Ok(HexOrBytes(b));
        }
        // Fall back to hex string
        if let Ok(s) = ob.extract::<String>() {
            if s.is_empty() {
                return Ok(HexOrBytes(Vec::new()));
            }
            let bytes: Result<Vec<u8>, _> = (0..s.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&s[i..i.min(s.len()).max(i + 2)], 16))
                .collect();
            return bytes
                .map(HexOrBytes)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(
                    format!("Invalid hex string: {e}")));
        }
        Err(pyo3::exceptions::PyTypeError::new_err(
            "Expected bytes or hex string"))
    }
}

#[pyfunction]
#[pyo3(signature = (data, changes, signature=None, vanilla_data=None))]
fn apply_byte_patches(
    py: Python<'_>,
    data: &[u8],
    changes: Vec<ByteChangePy>,
    signature: Option<&[u8]>,
    vanilla_data: Option<&[u8]>,
) -> PyResult<(PyObject, u32, u32, u32)> {
    let mut buf = data.to_vec();
    let rust_changes: Vec<patcher::ByteChange> = changes
        .into_iter()
        .map(|c| patcher::ByteChange {
            offset: c.offset,
            original: c.original.0,
            patched: c.patched.0,
            change_type: if c.change_type == "insert" {
                patcher::ChangeType::Insert
            } else {
                patcher::ChangeType::Replace
            },
        })
        .collect();

    let (applied, mismatched, relocated) = py.allow_threads(|| {
        patcher::apply_byte_patches(&mut buf, &rust_changes, signature, vanilla_data)
    });

    let py_bytes = pyo3::types::PyBytes::new(py, &buf);
    Ok((py_bytes.into(), applied, mismatched, relocated))
}

// ── PAMT parsing ────────────────────────────────────────────────────

#[pyfunction]
fn parse_pamt(py: Python<'_>, data: &[u8]) -> PyResult<PyObject> {
    let entries = paz::parse_pamt(data)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;

    let list = pyo3::types::PyList::empty(py);
    for entry in entries {
        let dict = PyDict::new(py);
        dict.set_item("path", &entry.path)?;
        dict.set_item("paz_index", entry.paz_index)?;
        dict.set_item("offset", entry.offset)?;
        dict.set_item("comp_size", entry.comp_size)?;
        dict.set_item("orig_size", entry.orig_size)?;
        dict.set_item("flags", entry.flags)?;
        dict.set_item("compression_type", entry.compression_type())?;
        dict.set_item("is_compressed", entry.is_compressed())?;
        dict.set_item("is_encrypted", entry.is_encrypted())?;
        list.append(dict)?;
    }
    Ok(list.into())
}

// ── PAZ entry extraction ────────────────────────────────────────────

#[pyfunction]
fn extract_entry(
    py: Python<'_>,
    paz_path: &str,
    offset: u64,
    comp_size: u32,
    orig_size: u32,
    compression_type: u32,
    entry_path: &str,
) -> PyResult<PyObject> {
    let paz = paz_path.to_string();
    let epath = entry_path.to_string();

    let result = py.allow_threads(move || -> Result<Vec<u8>, String> {
        // Read only the needed bytes from PAZ (not the whole 900MB+ file)
        use std::io::{Read, Seek, SeekFrom};
        let mut file = std::fs::File::open(&paz)
            .map_err(|e| format!("Failed to open {}: {}", paz, e))?;
        file.seek(SeekFrom::Start(offset))
            .map_err(|e| format!("Failed to seek in {}: {}", paz, e))?;
        let mut entry_data = vec![0u8; comp_size as usize];
        file.read_exact(&mut entry_data)
            .map_err(|e| format!("Failed to read {} bytes from {}: {}", comp_size, paz, e))?;

        // Decrypt if needed
        let basename = epath.rsplit('/').next().unwrap_or(&epath);
        if crypto::is_encrypted_extension(basename) {
            crypto::decrypt_in_place(&mut entry_data, basename);
        }

        // Decompress based on type
        match compression_type {
            1 => {
                // DDS split
                dds::decompress_dds(&entry_data, orig_size)
            }
            2 => {
                // LZ4
                lz4_util::decompress(&entry_data, orig_size as usize)
            }
            _ => Ok(entry_data),
        }
    });

    match result {
        Ok(data) => Ok(pyo3::types::PyBytes::new(py, &data).into()),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ── Overlay builder ─────────────────────────────────────────────────

/// Input dict: {"dir_path": str, "filename": str, "content": bytes, "compression_type": int}
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct OverlayEntryPy {
    dir_path: String,
    filename: String,
    content: Vec<u8>,
    compression_type: u32,
}

#[pyfunction]
fn build_overlay_paz(py: Python<'_>, entries: Vec<OverlayEntryPy>) -> PyResult<PyObject> {
    let inputs: Vec<overlay::OverlayInput> = entries
        .into_iter()
        .map(|e| overlay::OverlayInput {
            dir_path: e.dir_path,
            filename: e.filename,
            content: e.content,
            compression_type: e.compression_type,
        })
        .collect();

    let result = py.allow_threads(|| overlay::build_overlay(inputs));

    let dict = PyDict::new(py);
    dict.set_item("paz_bytes", pyo3::types::PyBytes::new(py, &result.paz_bytes))?;
    dict.set_item("pamt_bytes", pyo3::types::PyBytes::new(py, &result.pamt_bytes))?;
    dict.set_item("entry_count", result.entry_count)?;
    Ok(dict.into())
}

// ── ENTR per-entry raw-bytes filter ─────────────────────────────────

/// Input tuple: (paz_index, mod_offset, mod_comp_size, van_offset, van_comp_size)
#[derive(FromPyObject)]
struct EntryPairPy(u32, u64, u64, u64, u64);

/// For each same-offset-same-size entry, seek+read the raw bytes in both
/// PAZ files and compare. Return the indices of entries where the raw
/// bytes differ (these are the ones that need full extraction + delta
/// generation on the Python side). Entries with offset/size mismatch are
/// always returned (the caller pre-filtered).
///
/// Replaces a Python per-entry loop that did 4 file-I/O syscalls +
/// byte comparison per entry. On a PAZ with 115K entries where only 1
/// changed, the Python loop took 13.7s. The Rust version batches the I/O
/// and uses native memcmp, running the same work in under a second.
#[pyfunction]
fn filter_changed_entries(
    py: Python<'_>,
    mod_paz_path: &str,
    van_paz_path: &str,
    entries: Vec<EntryPairPy>,
) -> PyResult<Vec<usize>> {
    let mod_path = mod_paz_path.to_string();
    let van_path = van_paz_path.to_string();

    let result: Result<Vec<usize>, String> = py.allow_threads(move || {
        use std::fs::File;
        use std::io::{Read, Seek, SeekFrom};

        let mut mod_f = File::open(&mod_path)
            .map_err(|e| format!("Failed to open {}: {}", mod_path, e))?;
        let mut van_f = File::open(&van_path)
            .map_err(|e| format!("Failed to open {}: {}", van_path, e))?;

        let mut changed: Vec<usize> = Vec::new();
        // Reused buffers. Grow as needed; most entries are <1MB.
        let mut mod_buf: Vec<u8> = Vec::new();
        let mut van_buf: Vec<u8> = Vec::new();

        for (idx, EntryPairPy(_paz_idx, mod_off, mod_sz, van_off, van_sz))
            in entries.iter().enumerate()
        {
            // Caller passes same-offset-same-size entries only. If they
            // ever differ we can't do a raw-bytes compare — flag as changed.
            if mod_sz != van_sz {
                changed.push(idx);
                continue;
            }

            let sz = *mod_sz as usize;
            if mod_buf.len() < sz {
                mod_buf.resize(sz, 0);
            }
            if van_buf.len() < sz {
                van_buf.resize(sz, 0);
            }

            mod_f.seek(SeekFrom::Start(*mod_off))
                .map_err(|e| format!("mod seek: {}", e))?;
            mod_f.read_exact(&mut mod_buf[..sz])
                .map_err(|e| format!("mod read: {}", e))?;

            van_f.seek(SeekFrom::Start(*van_off))
                .map_err(|e| format!("van seek: {}", e))?;
            van_f.read_exact(&mut van_buf[..sz])
                .map_err(|e| format!("van read: {}", e))?;

            if mod_buf[..sz] != van_buf[..sz] {
                changed.push(idx);
            }
        }

        Ok(changed)
    });

    result.map_err(pyo3::exceptions::PyRuntimeError::new_err)
}

// ── Sparse diff finder ──────────────────────────────────────────────

/// Find runs of differing bytes between two equal-length slices.
///
/// Returns a list of (absolute_offset, diff_bytes) for each contiguous
/// range where `va[i] != vb[i]`. Used by the streaming sparse-delta
/// generator to replace a Python byte-by-byte loop that was running
/// 1M+ iterations per 1MB chunk on large PAZ mods.
///
/// `base_offset` is added to the in-slice position so callers can feed
/// in chunks and get back absolute offsets into the original file.
#[pyfunction]
fn find_sparse_diffs(
    py: Python<'_>,
    va: &[u8],
    vb: &[u8],
    base_offset: u64,
) -> PyResult<PyObject> {
    // Compute ranges without holding the GIL so parallel callers could
    // overlap. The diff list itself stays small (few hundred ranges at
    // worst), so allocating here is cheap.
    let patches: Vec<(u64, Vec<u8>)> = py.allow_threads(|| {
        let len = va.len().min(vb.len());
        let mut out: Vec<(u64, Vec<u8>)> = Vec::new();
        let mut i = 0usize;
        while i < len {
            if va[i] == vb[i] {
                i += 1;
                continue;
            }
            let start = i;
            while i < len && va[i] != vb[i] {
                i += 1;
            }
            out.push((base_offset + start as u64, vb[start..i].to_vec()));
        }
        out
    });

    let list = pyo3::types::PyList::empty(py);
    for (off, data) in patches {
        let tup = pyo3::types::PyTuple::new(
            py,
            &[
                off.into_pyobject(py)?.into_any().unbind(),
                pyo3::types::PyBytes::new(py, &data).into_any().unbind(),
            ],
        )?;
        list.append(tup)?;
    }
    Ok(list.into())
}

// ── Module registration ─────────────────────────────────────────────

#[pymodule]
fn cdumm_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("HASH_SEED", hashlittle::HASH_SEED)?;

    // Hash functions
    m.add_function(wrap_pyfunction!(compute_hashlittle, m)?)?;
    m.add_function(wrap_pyfunction!(compute_pa_checksum, m)?)?;

    // LZ4
    m.add_function(wrap_pyfunction!(lz4_compress, m)?)?;
    m.add_function(wrap_pyfunction!(lz4_decompress, m)?)?;

    // ChaCha20
    m.add_function(wrap_pyfunction!(chacha20_encrypt, m)?)?;
    m.add_function(wrap_pyfunction!(chacha20_decrypt, m)?)?;
    m.add_function(wrap_pyfunction!(derive_key_iv, m)?)?;
    m.add_function(wrap_pyfunction!(is_encrypted_extension, m)?)?;

    // Pattern scan + patching
    m.add_function(wrap_pyfunction!(pattern_scan, m)?)?;
    m.add_function(wrap_pyfunction!(apply_byte_patches, m)?)?;

    // PAMT parsing
    m.add_function(wrap_pyfunction!(parse_pamt, m)?)?;

    // PAZ entry extraction
    m.add_function(wrap_pyfunction!(extract_entry, m)?)?;

    // Overlay builder
    m.add_function(wrap_pyfunction!(build_overlay_paz, m)?)?;

    // Sparse diff finder (streaming delta hot loop)
    m.add_function(wrap_pyfunction!(find_sparse_diffs, m)?)?;

    // ENTR per-entry raw-bytes filter
    m.add_function(wrap_pyfunction!(filter_changed_entries, m)?)?;

    Ok(())
}
