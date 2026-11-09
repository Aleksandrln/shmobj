//! Нормализованный ключ и `key_hash64` (doc/format.md §8).
//!
//! `1`, `1.0` и `True` нормализуются в одно [`IntKey`], поэтому сравнение и хэш
//! совпадают с семантикой `dict`.

use std::borrow::Cow;

use xxhash_rust::xxh3::Xxh3;

use super::scalar::{big_float_int_bytes, i128_le_bytes, le_bytes_to_i128};

/// Целое значение ключа. Инвариант: `Big` только для чисел вне i128.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum IntKey<'a> {
    Small(i128),
    Big(Cow<'a, [u8]>),
}

impl<'a> IntKey<'a> {
    /// Из байт дополнения до двух (LE).
    pub fn from_le(b: &'a [u8]) -> IntKey<'a> {
        match le_bytes_to_i128(b) {
            Some(v) => IntKey::Small(v),
            None => IntKey::Big(Cow::Borrowed(b)),
        }
    }

    fn hash_into(&self, h: &mut Xxh3) {
        match self {
            IntKey::Small(v) => {
                let (buf, n) = i128_le_bytes(*v);
                h.update(buf.get(..n).unwrap_or(&[]));
            }
            IntKey::Big(b) => h.update(b),
        }
    }

    pub fn into_owned(self) -> IntKey<'static> {
        match self {
            IntKey::Small(v) => IntKey::Small(v),
            IntKey::Big(b) => IntKey::Big(Cow::Owned(b.into_owned())),
        }
    }
}

/// Ключ в нормальной форме: числа с целым значением — всегда `Int`.
#[derive(Debug, Clone)]
pub enum Key<'a> {
    None,
    Int(IntKey<'a>),
    /// Только дробные и ±inf, не NaN.
    Float(f64),
    Str(Cow<'a, [u8]>),
    Bytes(Cow<'a, [u8]>),
}

impl<'a> Key<'a> {
    /// `None` для NaN: такой ключ не может существовать.
    pub fn from_f64(f: f64) -> Option<Key<'a>> {
        if f.is_nan() {
            return None;
        }
        if f.is_finite() && f.fract() == 0.0 {
            if f.abs() < 2f64.powi(127) {
                return Some(Key::Int(IntKey::Small(f as i128)));
            }
            let b = big_float_int_bytes(f);
            return Some(Key::Int(match le_bytes_to_i128(&b) {
                Some(v) => IntKey::Small(v),
                None => IntKey::Big(Cow::Owned(b)),
            }));
        }
        Some(Key::Float(f))
    }

    pub fn into_owned(self) -> Key<'static> {
        match self {
            Key::None => Key::None,
            Key::Int(i) => Key::Int(i.into_owned()),
            Key::Float(f) => Key::Float(f),
            Key::Str(s) => Key::Str(Cow::Owned(s.into_owned())),
            Key::Bytes(b) => Key::Bytes(Cow::Owned(b.into_owned())),
        }
    }
}

impl PartialEq for Key<'_> {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Key::None, Key::None) => true,
            (Key::Int(a), Key::Int(b)) => a == b,
            (Key::Float(a), Key::Float(b)) => a == b,
            (Key::Str(a), Key::Str(b)) => a == b,
            (Key::Bytes(a), Key::Bytes(b)) => a == b,
            _ => false,
        }
    }
}

impl Eq for Key<'_> {}

impl std::hash::Hash for Key<'_> {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        state.write_u64(key_hash64(self));
    }
}

/// `xxh3_64(type_byte ‖ payload)`, seed 0.
pub fn key_hash64(k: &Key<'_>) -> u64 {
    let mut h = Xxh3::new();
    match k {
        Key::None => h.update(&[0x00]),
        Key::Int(i) => {
            h.update(&[0x01]);
            i.hash_into(&mut h);
        }
        Key::Float(f) => {
            h.update(&[0x02]);
            h.update(&f.to_le_bytes());
        }
        Key::Str(s) => {
            h.update(&[0x03]);
            h.update(s);
        }
        Key::Bytes(b) => {
            h.update(&[0x04]);
            h.update(b);
        }
    }
    h.digest()
}
