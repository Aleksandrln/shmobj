//! DICT и KEYTABLE (doc/spec/03-format.md §6–7).

use crate::format::error::{FormatError, Result};
use crate::format::hash::{key_hash64, Key};
use crate::format::slot::Slot;

use super::reader::{Seg, ValueRef};
use super::{NODE_DICT, NODE_KEYTABLE};

pub const KT_LINEAR: u8 = 0;
pub const KT_SORTED32: u8 = 1;

#[derive(Debug, Clone, Copy)]
pub struct KeyTable<'a> {
    seg: Seg<'a>,
    pub off: u64,
    pub sorted: bool,
    pub len: u32,
}

impl<'a> KeyTable<'a> {
    pub fn open(seg: Seg<'a>, off: u64) -> Result<KeyTable<'a>> {
        let (sub, len) = seg.container(off, NODE_KEYTABLE)?;
        let sorted = match sub {
            KT_LINEAR => false,
            KT_SORTED32 => true,
            _ => return Err(FormatError::at(format!("неизвестный подтип KEYTABLE {sub}"), off)),
        };
        let per_key = if sorted { 16 } else { 8 };
        seg.need(off, 8 + u64::from(len) * per_key)?;
        Ok(KeyTable { seg, off, sorted, len })
    }

    pub fn key_slot(&self, i: usize) -> Result<Slot> {
        if i >= self.len as usize {
            return Err(FormatError::at("индекс ключа вне таблицы", self.off));
        }
        Ok(Slot(self.seg.u64_at(self.off + 8 + 8 * i as u64)?))
    }

    /// Запись индекса SORTED32: `(h32 << 32) | entry`.
    pub fn index_entry(&self, j: usize) -> Result<u64> {
        if !self.sorted || j >= self.len as usize {
            return Err(FormatError::at("индекс вне таблицы", self.off));
        }
        self.seg.u64_at(self.off + 8 + 8 * u64::from(self.len) + 8 * j as u64)
    }

    /// Позиция ключа в `keys`.
    pub fn find(&self, q: &Key<'_>) -> Result<Option<usize>> {
        let n = self.len as usize;
        if !self.sorted {
            for i in 0..n {
                if self.seg.key_eq(self.key_slot(i)?, q)? {
                    return Ok(Some(i));
                }
            }
            return Ok(None);
        }
        let h32 = key_hash64(q) >> 32;
        let (mut lo, mut hi) = (0usize, n);
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if self.index_entry(mid)? >> 32 < h32 {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        while lo < n {
            let e = self.index_entry(lo)?;
            if e >> 32 != h32 {
                break;
            }
            let i = (e & 0xFFFF_FFFF) as usize;
            if self.seg.key_eq(self.key_slot(i)?, q)? {
                return Ok(Some(i));
            }
            lo += 1;
        }
        Ok(None)
    }
}

#[derive(Debug, Clone, Copy)]
pub struct DictView<'a> {
    seg: Seg<'a>,
    pub off: u64,
    pub len: u32,
    pub keys: KeyTable<'a>,
}

impl<'a> DictView<'a> {
    pub fn open(seg: Seg<'a>, off: u64) -> Result<DictView<'a>> {
        let (_, len) = seg.container(off, NODE_DICT)?;
        seg.need(off, 16 + 8 * u64::from(len))?;
        let kt = seg.u64_at(off + 8)?;
        if kt >= off {
            return Err(FormatError::at("ссылка вперёд (нарушен пост-порядок)", off));
        }
        let keys = KeyTable::open(seg, kt)?;
        if keys.len != len {
            return Err(FormatError::at("длина DICT не равна длине KEYTABLE", off));
        }
        Ok(DictView { seg, off, len, keys })
    }

    pub fn len(&self) -> usize {
        self.len as usize
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn value_slot(&self, i: usize) -> Result<Slot> {
        if i >= self.len() {
            return Err(FormatError::at("индекс значения вне словаря", self.off));
        }
        Ok(Slot(self.seg.u64_at(self.off + 16 + 8 * i as u64)?))
    }

    pub fn key(&self, i: usize) -> Result<ValueRef<'a>> {
        self.seg.decode(self.keys.key_slot(i)?)
    }

    pub fn value(&self, i: usize) -> Result<ValueRef<'a>> {
        self.seg.decode(self.value_slot(i)?)
    }

    pub fn get(&self, q: &Key<'_>) -> Result<Option<ValueRef<'a>>> {
        match self.keys.find(q)? {
            Some(i) => self.value(i).map(Some),
            None => Ok(None),
        }
    }
}
