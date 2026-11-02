//! Заголовок сегмента (64 байта), doc/spec/03-format.md §1.

use super::error::{FormatError, Result};
use super::{read_u32, read_u64};

pub const MAGIC: &[u8; 4] = b"SHMO";
pub const FORMAT_VERSION: u16 = 1;
pub const HASH_XXH3_64: u8 = 1;
pub const HEADER_SIZE: usize = 64;
pub const FLAG_CHECKSUM: u32 = 1;
/// Конец значимых полей; `[RESERVED_START, HEADER_SIZE)` — резерв под RW.
const RESERVED_START: usize = 40;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Ro,
    Rw,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub kind: Kind,
    pub flags: u32,
    pub data_size: u64,
    pub root: u64,
    pub checksum: u64,
}

impl Header {
    /// Разбирает заголовок. Единственное место ветвления по `kind`:
    /// RW пока не поддерживается и отклоняется здесь.
    pub fn parse(buf: &[u8]) -> Result<Header> {
        let h = buf
            .get(..HEADER_SIZE)
            .ok_or_else(|| FormatError::new("буфер меньше заголовка"))?;
        if h.get(0..4) != Some(MAGIC.as_slice()) {
            return Err(FormatError::new("неверный magic"));
        }
        let version = u16::from_le_bytes([h.get(4).copied().unwrap_or(0), h.get(5).copied().unwrap_or(0)]);
        if version != FORMAT_VERSION {
            return Err(FormatError::new(format!("неподдерживаемая версия формата {version}")));
        }
        let kind = match h.get(6).copied() {
            Some(0) => Kind::Ro,
            Some(1) => return Err(FormatError::new("сегмент RW (kind=1), нужна RW-версия shmobj")),
            k => return Err(FormatError::new(format!("неизвестный kind {}", k.unwrap_or(0)))),
        };
        let hash_algo = h.get(7).copied();
        let header_size = read_u32(h, 8);
        if hash_algo != Some(HASH_XXH3_64) || header_size != Some(HEADER_SIZE as u32) {
            return Err(FormatError::new("неподдерживаемый hash_algo/header_size"));
        }
        let flags = read_u32(h, 12).unwrap_or(0);
        if flags & !FLAG_CHECKSUM != 0 {
            return Err(FormatError::new("неизвестные биты flags"));
        }
        if h.get(RESERVED_START..).is_some_and(|r| r.iter().any(|&b| b != 0)) {
            return Err(FormatError::new("резерв заголовка не нулевой"));
        }
        let data_size = read_u64(h, 16).unwrap_or(0);
        if data_size < HEADER_SIZE as u64 || data_size > buf.len() as u64 {
            return Err(FormatError::new("data_size вне буфера"));
        }
        Ok(Header {
            kind,
            flags,
            data_size,
            root: read_u64(h, 24).unwrap_or(0),
            checksum: read_u64(h, 32).unwrap_or(0),
        })
    }

    pub fn has_checksum(&self) -> bool {
        self.flags & FLAG_CHECKSUM != 0
    }

    pub fn write(&self, out: &mut [u8]) {
        let kind = match self.kind {
            Kind::Ro => 0u8,
            Kind::Rw => 1u8,
        };
        let mut h = [0u8; HEADER_SIZE];
        let fields: [(usize, &[u8]); 9] = [
            (0, MAGIC),
            (4, &FORMAT_VERSION.to_le_bytes()),
            (6, &[kind]),
            (7, &[HASH_XXH3_64]),
            (8, &(HEADER_SIZE as u32).to_le_bytes()),
            (12, &self.flags.to_le_bytes()),
            (16, &self.data_size.to_le_bytes()),
            (24, &self.root.to_le_bytes()),
            (32, &self.checksum.to_le_bytes()),
        ];
        for (off, bytes) in fields {
            if let Some(dst) = h.get_mut(off..off + bytes.len()) {
                dst.copy_from_slice(bytes);
            }
        }
        if let Some(dst) = out.get_mut(..HEADER_SIZE) {
            dst.copy_from_slice(&h);
        }
    }
}

/// xxh3_64 тела сегмента `[64, data_size)`.
pub fn body_checksum(buf: &[u8], data_size: u64) -> u64 {
    let body = usize::try_from(data_size)
        .ok()
        .and_then(|end| buf.get(HEADER_SIZE..end))
        .unwrap_or(&[]);
    xxhash_rust::xxh3::xxh3_64(body)
}
