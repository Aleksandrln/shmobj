//! Слот: `tag = slot & 0xF`, `payload = slot >> 4` (doc/spec/03-format.md §2).

use super::error::{FormatError, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Tag {
    None = 0,
    False = 1,
    True = 2,
    SmallInt = 3,
    Int64 = 4,
    BigInt = 5,
    Float = 6,
    Str = 7,
    Bytes = 8,
    List = 9,
    Dict = 10,
    ShortStr = 11,
}

impl Tag {
    /// Теги 12–15 зарезервированы под RW и в RO дают ошибку.
    pub fn from_u8(t: u8) -> Result<Tag> {
        Ok(match t {
            0 => Tag::None,
            1 => Tag::False,
            2 => Tag::True,
            3 => Tag::SmallInt,
            4 => Tag::Int64,
            5 => Tag::BigInt,
            6 => Tag::Float,
            7 => Tag::Str,
            8 => Tag::Bytes,
            9 => Tag::List,
            10 => Tag::Dict,
            11 => Tag::ShortStr,
            _ => return Err(FormatError::new(format!("тег {t} недопустим в RO"))),
        })
    }

    /// Слот-ссылка на узел (payload — смещение).
    pub fn is_ref(self) -> bool {
        matches!(
            self,
            Tag::Int64 | Tag::BigInt | Tag::Float | Tag::Str | Tag::Bytes | Tag::List | Tag::Dict
        )
    }
}

pub const SMALLINT_MIN: i64 = -(1 << 59);
pub const SMALLINT_MAX: i64 = (1 << 59) - 1;
pub const SHORTSTR_MAX: usize = 7;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Slot(pub u64);

impl Slot {
    pub const NONE: Slot = Slot(Tag::None as u64);
    pub const FALSE: Slot = Slot(Tag::False as u64);
    pub const TRUE: Slot = Slot(Tag::True as u64);

    pub fn raw_tag(self) -> u8 {
        (self.0 & 0xF) as u8
    }

    pub fn tag(self) -> Result<Tag> {
        Tag::from_u8(self.raw_tag())
    }

    pub fn payload(self) -> u64 {
        self.0 >> 4
    }

    pub fn bool(v: bool) -> Slot {
        if v {
            Slot::TRUE
        } else {
            Slot::FALSE
        }
    }

    pub fn reference(tag: Tag, offset: u64) -> Slot {
        Slot((offset << 4) | tag as u64)
    }

    pub fn small_int(v: i64) -> Option<Slot> {
        (SMALLINT_MIN..=SMALLINT_MAX)
            .contains(&v)
            .then_some(Slot(((v << 4) as u64) | Tag::SmallInt as u64))
    }

    /// Значение SMALLINT: арифметический сдвиг.
    pub fn small_int_value(self) -> i64 {
        (self.0 as i64) >> 4
    }

    pub fn short_str(bytes: &[u8]) -> Option<Slot> {
        if bytes.len() > SHORTSTR_MAX {
            return None;
        }
        let mut v: u64 = 0;
        for (i, &b) in bytes.iter().enumerate() {
            v |= u64::from(b) << (8 * i);
        }
        Some(Slot((v << 8) | ((bytes.len() as u64) << 4) | Tag::ShortStr as u64))
    }

    /// Байты SHORTSTR без проверки UTF-8; неиспользуемые биты обязаны быть нулями.
    pub fn short_str_bytes(self) -> Result<Inline> {
        let p = self.payload();
        let n = (p & 0xF) as usize;
        let data = p >> 4;
        if n > SHORTSTR_MAX || (data >> (8 * n)) != 0 {
            return Err(FormatError::new("битый SHORTSTR"));
        }
        Ok(Inline {
            buf: data.to_le_bytes(),
            len: n as u8,
        })
    }
}

/// До 8 байт, хранящихся по значению (SHORTSTR).
#[derive(Debug, Clone, Copy)]
pub struct Inline {
    buf: [u8; 8],
    len: u8,
}

impl Inline {
    pub fn as_slice(&self) -> &[u8] {
        self.buf.get(..self.len as usize).unwrap_or(&[])
    }
}
