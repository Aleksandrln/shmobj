//! Безопасный доступ к узлам сегмента: все чтения с проверкой границ.

use crate::format::error::{FormatError, Result};
use crate::format::hash::{IntKey, Key};
use crate::format::header::{Header, Kind, HEADER_SIZE};
use crate::format::slot::{Inline, Slot, Tag};
use crate::format::{read_u32, read_u64, ALIGN};

use super::dict::DictView;
use super::list::ListView;

/// Байты сегмента `[0, data_size)`.
#[derive(Debug, Clone, Copy)]
pub struct Seg<'a> {
    buf: &'a [u8],
}

/// Декодированный слот. Строки отдаются байтами: проверка UTF-8 — на стороне потребителя.
#[derive(Debug, Clone, Copy)]
pub enum ValueRef<'a> {
    None,
    Bool(bool),
    Int(i64),
    /// Дополнение до двух, LE.
    BigInt(&'a [u8]),
    Float(f64),
    Str {
        utf8: &'a [u8],
        char_count: u32,
    },
    ShortStr(Inline),
    Bytes(&'a [u8]),
    List(ListView<'a>),
    Dict(DictView<'a>),
}

impl<'a> Seg<'a> {
    /// Разбирает заголовок; только RO.
    pub fn open(buf: &'a [u8]) -> Result<(Seg<'a>, Header)> {
        let h = Header::parse(buf)?;
        if h.kind != Kind::Ro {
            return Err(FormatError::new("сегмент не RO"));
        }
        let end = usize::try_from(h.data_size).map_err(|_| FormatError::new("data_size вне буфера"))?;
        let body = buf.get(..end).ok_or_else(|| FormatError::new("data_size вне буфера"))?;
        Ok((Seg { buf: body }, h))
    }

    /// Поверх `[0, data_size)` уже проверенного заголовком буфера.
    pub fn from_checked(buf: &'a [u8]) -> Seg<'a> {
        Seg { buf }
    }

    /// Весь сегмент `[0, data_size)`, включая заголовок.
    pub fn bytes_all(&self) -> &'a [u8] {
        self.buf
    }

    pub fn size(&self) -> u64 {
        self.buf.len() as u64
    }

    /// Узел `[off, off + n)` целиком внутри `[64, data_size)`, `off` выровнен.
    pub fn need(&self, off: u64, n: u64) -> Result<usize> {
        let ok = off >= HEADER_SIZE as u64
            && off.is_multiple_of(ALIGN)
            && off.checked_add(n).is_some_and(|end| end <= self.size());
        if !ok {
            return Err(FormatError::at(format!("выход за границы: n={n}"), off));
        }
        usize::try_from(off).map_err(|_| FormatError::at("смещение вне адресного пространства", off))
    }

    pub fn bytes(&self, off: u64, n: u64) -> Result<&'a [u8]> {
        let start = self.need(off, n)?;
        let end = start
            .checked_add(n as usize)
            .ok_or_else(|| FormatError::at("переполнение", off))?;
        self.buf
            .get(start..end)
            .ok_or_else(|| FormatError::at("выход за границы", off))
    }

    /// Чтение по смещению без требования выравнивания (поля внутри проверенного узла).
    pub fn u32_at(&self, off: u64) -> Result<u32> {
        usize::try_from(off)
            .ok()
            .and_then(|o| read_u32(self.buf, o))
            .ok_or_else(|| FormatError::at("выход за границы", off))
    }

    pub fn u64_at(&self, off: u64) -> Result<u64> {
        usize::try_from(off)
            .ok()
            .and_then(|o| read_u64(self.buf, o))
            .ok_or_else(|| FormatError::at("выход за границы", off))
    }

    /// Заголовок контейнера ожидаемого типа -> (subtype, len).
    pub fn container(&self, off: u64, expect: u8) -> Result<(u8, u32)> {
        let h = self.bytes(off, 8)?;
        let node_type = h.first().copied().unwrap_or(0);
        if node_type != expect {
            return Err(FormatError::at(
                format!("ожидался узел {expect}, найден {node_type}"),
                off,
            ));
        }
        let sub = h.get(1).copied().unwrap_or(0);
        let len = self.u32_at(off + 4)?;
        Ok((sub, len))
    }

    pub fn decode(&self, s: Slot) -> Result<ValueRef<'a>> {
        let p = s.payload();
        Ok(match s.tag()? {
            Tag::None => ValueRef::None,
            Tag::False => ValueRef::Bool(false),
            Tag::True => ValueRef::Bool(true),
            Tag::SmallInt => ValueRef::Int(s.small_int_value()),
            Tag::ShortStr => ValueRef::ShortStr(s.short_str_bytes()?),
            Tag::Int64 => {
                self.need(p, 8)?;
                ValueRef::Int(self.u64_at(p)? as i64)
            }
            Tag::Float => {
                self.need(p, 8)?;
                ValueRef::Float(f64::from_bits(self.u64_at(p)?))
            }
            Tag::BigInt => {
                self.need(p, 8)?;
                let n = self.u32_at(p)?;
                self.need(p, 8 + u64::from(n))?;
                ValueRef::BigInt(self.bytes_inner(p + 8, u64::from(n))?)
            }
            Tag::Str => {
                self.need(p, 8)?;
                let n = self.u32_at(p)?;
                let char_count = self.u32_at(p + 4)?;
                self.need(p, 8 + u64::from(n))?;
                ValueRef::Str {
                    utf8: self.bytes_inner(p + 8, u64::from(n))?,
                    char_count,
                }
            }
            Tag::Bytes => {
                self.need(p, 8)?;
                let n = self.u64_at(p)?;
                let total = n
                    .checked_add(8)
                    .ok_or_else(|| FormatError::at("переполнение длины", p))?;
                self.need(p, total)?;
                ValueRef::Bytes(self.bytes_inner(p + 8, n)?)
            }
            Tag::List => ValueRef::List(ListView::open(*self, p)?),
            Tag::Dict => ValueRef::Dict(DictView::open(*self, p)?),
        })
    }

    /// Байты внутри уже проверенного узла (без требования выравнивания).
    fn bytes_inner(&self, off: u64, n: u64) -> Result<&'a [u8]> {
        let start = usize::try_from(off).map_err(|_| FormatError::at("выход за границы", off))?;
        let len = usize::try_from(n).map_err(|_| FormatError::at("выход за границы", off))?;
        start
            .checked_add(len)
            .and_then(|end| self.buf.get(start..end))
            .ok_or_else(|| FormatError::at("выход за границы", off))
    }

    /// Слот ключа в нормальной форме. Контейнеры и NaN — ошибка.
    pub fn decode_key(&self, s: Slot) -> Result<Key<'a>> {
        Ok(match self.decode(s)? {
            ValueRef::None => Key::None,
            ValueRef::Bool(b) => Key::Int(IntKey::Small(i128::from(b))),
            ValueRef::Int(v) => Key::Int(IntKey::Small(i128::from(v))),
            ValueRef::BigInt(b) => Key::Int(IntKey::from_le(b)),
            ValueRef::Float(f) => Key::from_f64(f).ok_or_else(|| FormatError::new("NaN в роли ключа"))?,
            ValueRef::Str { utf8, .. } => Key::Str(utf8.into()),
            ValueRef::ShortStr(inl) => Key::Str(inl.as_slice().to_vec().into()),
            ValueRef::Bytes(b) => Key::Bytes(b.into()),
            ValueRef::List(_) | ValueRef::Dict(_) => return Err(FormatError::new("контейнер в роли ключа")),
        })
    }

    /// Равен ли ключ в слоте `s` ключу запроса (без аллокаций для строк и целых).
    pub fn key_eq(&self, s: Slot, q: &Key<'_>) -> Result<bool> {
        let tag = s.tag()?;
        Ok(match q {
            Key::None => tag == Tag::None,
            Key::Str(qs) => match tag {
                Tag::ShortStr => s.short_str_bytes()?.as_slice() == qs.as_ref(),
                Tag::Str => matches!(self.decode(s)?, ValueRef::Str { utf8, .. } if utf8 == qs.as_ref()),
                _ => false,
            },
            Key::Bytes(qb) => tag == Tag::Bytes && matches!(self.decode(s)?, ValueRef::Bytes(b) if b == qb.as_ref()),
            Key::Float(qf) => tag == Tag::Float && matches!(self.decode(s)?, ValueRef::Float(f) if f == *qf),
            Key::Int(qi) => match tag {
                Tag::False => *qi == IntKey::Small(0),
                Tag::True => *qi == IntKey::Small(1),
                Tag::SmallInt => *qi == IntKey::Small(i128::from(s.small_int_value())),
                Tag::Int64 | Tag::BigInt | Tag::Float => match self.decode_key(s) {
                    Ok(Key::Int(k)) => k == *qi,
                    Ok(_) => false,
                    Err(e) => return Err(e),
                },
                _ => false,
            },
        })
    }
}
