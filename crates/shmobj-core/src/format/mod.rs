//! Общее для RO и будущего RW: заголовок, слоты, узлы скаляров, хэш ключей.
//! Не зависит от `ro/` (doc/spec/05-rw-groundwork.md §2).

pub mod error;
pub mod hash;
pub mod header;
pub mod scalar;
pub mod slot;

/// Выравнивание узлов.
pub const ALIGN: u64 = 8;

pub(crate) fn read_u32(buf: &[u8], off: usize) -> Option<u32> {
    let b = buf.get(off..off.checked_add(4)?)?;
    Some(u32::from_le_bytes(b.try_into().ok()?))
}

pub(crate) fn read_u64(buf: &[u8], off: usize) -> Option<u64> {
    let b = buf.get(off..off.checked_add(8)?)?;
    Some(u64::from_le_bytes(b.try_into().ok()?))
}
