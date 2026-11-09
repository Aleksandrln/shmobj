//! LIST: заголовок и `len` элементов (doc/format.md §5).

use crate::format::error::{FormatError, Result};
use crate::format::slot::Slot;

use super::reader::{Seg, ValueRef};
use super::NODE_LIST;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum ListSub {
    Slots = 0,
    I8 = 1,
    U8 = 2,
    I16 = 3,
    U16 = 4,
    I32 = 5,
    U32 = 6,
    I64 = 7,
    F64 = 8,
    Bool = 9,
}

impl ListSub {
    pub fn from_u8(v: u8) -> Result<ListSub> {
        use ListSub::*;
        Ok(match v {
            0 => Slots,
            1 => I8,
            2 => U8,
            3 => I16,
            4 => U16,
            5 => I32,
            6 => U32,
            7 => I64,
            8 => F64,
            9 => Bool,
            _ => return Err(FormatError::new(format!("неизвестный подтип LIST {v}"))),
        })
    }

    pub fn elem_size(self) -> u64 {
        use ListSub::*;
        match self {
            Slots | I64 | F64 => 8,
            I8 | U8 | Bool => 1,
            I16 | U16 => 2,
            I32 | U32 => 4,
        }
    }

    /// Минимальная ширина для целых в `[min, max]` в порядке U8, I8, U16, I16, U32, I32, I64.
    pub fn for_int_range(min: i64, max: i64) -> ListSub {
        use ListSub::*;
        let fits = |lo: i64, hi: i64| lo <= min && max <= hi;
        if fits(0, 0xFF) {
            U8
        } else if fits(-0x80, 0x7F) {
            I8
        } else if fits(0, 0xFFFF) {
            U16
        } else if fits(-0x8000, 0x7FFF) {
            I16
        } else if fits(0, 0xFFFF_FFFF) {
            U32
        } else if fits(-0x8000_0000, 0x7FFF_FFFF) {
            I32
        } else {
            I64
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ListView<'a> {
    seg: Seg<'a>,
    pub off: u64,
    pub sub: ListSub,
    pub len: u32,
}

impl<'a> ListView<'a> {
    pub fn open(seg: Seg<'a>, off: u64) -> Result<ListView<'a>> {
        let (sub, len) = seg.container(off, NODE_LIST)?;
        let sub = ListSub::from_u8(sub)?;
        seg.need(off, 8 + u64::from(len) * sub.elem_size())?;
        Ok(ListView { seg, off, sub, len })
    }

    pub fn len(&self) -> usize {
        self.len as usize
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    fn elem(&self, i: usize) -> Result<&'a [u8]> {
        if i >= self.len() {
            return Err(FormatError::at("индекс вне списка", self.off));
        }
        let size = self.sub.elem_size();
        let start = self.off + 8 + i as u64 * size;
        let all = self.seg.bytes(self.off, 8 + u64::from(self.len) * size)?;
        let rel = (start - self.off) as usize;
        all.get(rel..rel + size as usize)
            .ok_or_else(|| FormatError::at("выход за границы", self.off))
    }

    /// Слот элемента; только для SLOTS.
    pub fn slot(&self, i: usize) -> Result<Slot> {
        if self.sub != ListSub::Slots {
            return Err(FormatError::at("список типизирован", self.off));
        }
        let b = self.elem(i)?;
        Ok(Slot(u64::from_le_bytes(
            b.try_into().map_err(|_| FormatError::at("битый слот", self.off))?,
        )))
    }

    pub fn get(&self, i: usize) -> Result<ValueRef<'a>> {
        use ListSub::*;
        if self.sub == Slots {
            return self.seg.decode(self.slot(i)?);
        }
        let b = self.elem(i)?;
        let mut w = [0u8; 8];
        if let Some(dst) = w.get_mut(..b.len()) {
            dst.copy_from_slice(b);
        }
        let u = u64::from_le_bytes(w);
        Ok(match self.sub {
            I8 => ValueRef::Int(i64::from(u as u8 as i8)),
            U8 => ValueRef::Int(i64::from(u as u8)),
            I16 => ValueRef::Int(i64::from(u as u16 as i16)),
            U16 => ValueRef::Int(i64::from(u as u16)),
            I32 => ValueRef::Int(i64::from(u as u32 as i32)),
            U32 => ValueRef::Int(i64::from(u as u32)),
            I64 => ValueRef::Int(u as i64),
            F64 => ValueRef::Float(f64::from_bits(u)),
            Bool => ValueRef::Bool(u != 0),
            Slots => return Err(FormatError::at("недостижимо", self.off)),
        })
    }
}
