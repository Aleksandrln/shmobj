//! Сборка сегмента в `Vec<u8>` в пост-порядке: узел пишется после всего, на что ссылается.
//!
//! Порядок записи и дедупликация повторяют эталон (`reference/shmobj_ref.py`),
//! поэтому при одинаковом входе байты совпадают.

use std::collections::HashMap;

use crate::format::error::{FormatError, Result};
use crate::format::hash::{key_hash64, IntKey, Key};
use crate::format::header::{body_checksum, Header, Kind, FLAG_CHECKSUM, HEADER_SIZE};
use crate::format::scalar;
use crate::format::slot::{Slot, Tag};
use crate::format::ALIGN;

use super::dict::{KT_LINEAR, KT_SORTED32};
use super::list::ListSub;
use super::{LINEAR_MAX, MAX_LEN, NODE_DICT, NODE_KEYTABLE, NODE_LIST};

/// Ключ словаря на входе сборки (точный тип сохраняется).
#[derive(Debug, Clone, Copy)]
pub enum KeyIn<'a> {
    None,
    Bool(bool),
    Int(i64),
    /// Целое вне i64: байты по правилу `int.to_bytes((bit_length + 8) // 8, "little", signed=True)`.
    BigInt(&'a [u8]),
    Float(f64),
    Str {
        utf8: &'a str,
        char_count: usize,
    },
    Bytes(&'a [u8]),
}

#[derive(Hash, PartialEq, Eq)]
enum ScalarKey {
    I64(i64),
    Big(Vec<u8>),
    F64(u64),
    Str(Vec<u8>),
    Bytes(Vec<u8>),
}

/// Ключ дедупликации KEYTABLE: пара (точный тип, значение), как `(type(k), k)` в эталоне.
#[derive(Hash, PartialEq, Eq)]
enum KtKey {
    None,
    Bool(bool),
    Int(i64),
    Big(Vec<u8>),
    /// Биты f64; `-0.0` приводится к `0.0`, потому что в Python они равны.
    Float(u64),
    Str(Vec<u8>),
    Bytes(Vec<u8>),
}

impl KtKey {
    fn of(k: &KeyIn<'_>) -> KtKey {
        match *k {
            KeyIn::None => KtKey::None,
            KeyIn::Bool(b) => KtKey::Bool(b),
            KeyIn::Int(v) => KtKey::Int(v),
            KeyIn::BigInt(b) => KtKey::Big(b.to_vec()),
            KeyIn::Float(f) => KtKey::Float(if f == 0.0 { 0 } else { f.to_bits() }),
            KeyIn::Str { utf8, .. } => KtKey::Str(utf8.as_bytes().to_vec()),
            KeyIn::Bytes(b) => KtKey::Bytes(b.to_vec()),
        }
    }
}

pub struct Writer {
    out: Vec<u8>,
    dedup: bool,
    scalars: HashMap<ScalarKey, u64>,
    keytables: HashMap<Vec<KtKey>, u64>,
}

fn check_len(n: usize, what: &str) -> Result<u32> {
    u32::try_from(n)
        .ok()
        .filter(|&v| u64::from(v) <= MAX_LEN)
        .ok_or_else(|| FormatError::new(format!("{what}: больше 2^32-1")))
}

impl Writer {
    pub fn new(dedup: bool) -> Writer {
        Writer {
            out: vec![0u8; HEADER_SIZE],
            dedup,
            scalars: HashMap::new(),
            keytables: HashMap::new(),
        }
    }

    fn alloc(&mut self, write: impl FnOnce(&mut Vec<u8>)) -> u64 {
        let off = self.out.len() as u64;
        write(&mut self.out);
        let pad = (ALIGN as usize - self.out.len() % ALIGN as usize) % ALIGN as usize;
        self.out.resize(self.out.len() + pad, 0);
        off
    }

    fn scalar_node(&mut self, key: ScalarKey, write: impl FnOnce(&mut Vec<u8>)) -> u64 {
        if self.dedup {
            if let Some(&off) = self.scalars.get(&key) {
                return off;
            }
        }
        let off = self.alloc(write);
        if self.dedup {
            self.scalars.insert(key, off);
        }
        off
    }

    pub fn int(&mut self, v: i64) -> Slot {
        if let Some(s) = Slot::small_int(v) {
            return s;
        }
        let off = self.scalar_node(ScalarKey::I64(v), |o| scalar::encode_int64(o, v));
        Slot::reference(Tag::Int64, off)
    }

    pub fn bigint(&mut self, le: &[u8]) -> Result<Slot> {
        check_len(le.len(), "int")?;
        let off = self.scalar_node(ScalarKey::Big(le.to_vec()), |o| scalar::encode_bigint(o, le));
        Ok(Slot::reference(Tag::BigInt, off))
    }

    pub fn float(&mut self, f: f64) -> Slot {
        let off = self.scalar_node(ScalarKey::F64(f.to_bits()), |o| scalar::encode_float(o, f));
        Slot::reference(Tag::Float, off)
    }

    pub fn str(&mut self, s: &str, char_count: usize) -> Result<Slot> {
        let b = s.as_bytes();
        if let Some(slot) = Slot::short_str(b) {
            return Ok(slot);
        }
        check_len(b.len(), "str")?;
        let chars = check_len(char_count, "str")?;
        let off = self.scalar_node(ScalarKey::Str(b.to_vec()), |o| scalar::encode_str(o, b, chars));
        Ok(Slot::reference(Tag::Str, off))
    }

    pub fn bytes(&mut self, b: &[u8]) -> Slot {
        let off = self.scalar_node(ScalarKey::Bytes(b.to_vec()), |o| scalar::encode_bytes(o, b));
        Slot::reference(Tag::Bytes, off)
    }

    fn container(&mut self, node_type: u8, sub: u8, len: u32, body: impl FnOnce(&mut Vec<u8>)) -> u64 {
        self.alloc(|o| {
            o.extend_from_slice(&[node_type, sub, 0, 0]);
            o.extend_from_slice(&len.to_le_bytes());
            body(o);
        })
    }

    pub fn list_slots(&mut self, slots: &[Slot]) -> Result<Slot> {
        let n = check_len(slots.len(), "list")?;
        let off = self.container(NODE_LIST, ListSub::Slots as u8, n, |o| {
            for s in slots {
                o.extend_from_slice(&s.0.to_le_bytes());
            }
        });
        Ok(Slot::reference(Tag::List, off))
    }

    /// Однородный список `int` в минимальной ширине.
    pub fn list_ints(&mut self, vals: &[i64]) -> Result<Slot> {
        let n = check_len(vals.len(), "list")?;
        let min = vals.iter().copied().min().unwrap_or(0);
        let max = vals.iter().copied().max().unwrap_or(0);
        let sub = ListSub::for_int_range(min, max);
        let width = sub.elem_size() as usize;
        let off = self.container(NODE_LIST, sub as u8, n, |o| {
            for v in vals {
                o.extend_from_slice(v.to_le_bytes().get(..width).unwrap_or(&[]));
            }
        });
        Ok(Slot::reference(Tag::List, off))
    }

    pub fn list_floats(&mut self, vals: &[f64]) -> Result<Slot> {
        let n = check_len(vals.len(), "list")?;
        let off = self.container(NODE_LIST, ListSub::F64 as u8, n, |o| {
            for v in vals {
                o.extend_from_slice(&v.to_le_bytes());
            }
        });
        Ok(Slot::reference(Tag::List, off))
    }

    pub fn list_bools(&mut self, vals: &[bool]) -> Result<Slot> {
        let n = check_len(vals.len(), "list")?;
        let off = self.container(NODE_LIST, ListSub::Bool as u8, n, |o| {
            o.extend(vals.iter().map(|&b| u8::from(b)));
        });
        Ok(Slot::reference(Tag::List, off))
    }

    fn key_slot(&mut self, k: &KeyIn<'_>) -> Result<Slot> {
        Ok(match *k {
            KeyIn::None => Slot::NONE,
            KeyIn::Bool(b) => Slot::bool(b),
            KeyIn::Int(v) => self.int(v),
            KeyIn::BigInt(b) => self.bigint(b)?,
            KeyIn::Float(f) => self.float(f),
            KeyIn::Str { utf8, char_count } => self.str(utf8, char_count)?,
            KeyIn::Bytes(b) => self.bytes(b),
        })
    }

    /// Смещение KEYTABLE для последовательности ключей; одинаковые таблицы пишутся один раз.
    pub fn keytable(&mut self, keys: &[KeyIn<'_>]) -> Result<u64> {
        let dkey: Vec<KtKey> = keys.iter().map(KtKey::of).collect();
        if let Some(&off) = self.keytables.get(&dkey) {
            return Ok(off);
        }
        let n = check_len(keys.len(), "dict")?;
        let mut slots = Vec::with_capacity(keys.len());
        let mut index = Vec::new();
        let sorted = keys.len() > LINEAR_MAX;
        let normalized = keys.iter().map(norm_key).collect::<Result<Vec<_>>>()?;
        if sorted {
            for (i, k) in normalized.iter().enumerate() {
                index.push(((key_hash64(k) >> 32) << 32) | i as u64);
            }
        }
        for k in keys {
            slots.push(self.key_slot(k)?);
        }
        index.sort_unstable();
        let sub = if sorted { KT_SORTED32 } else { KT_LINEAR };
        let off = self.container(NODE_KEYTABLE, sub, n, |o| {
            for s in &slots {
                o.extend_from_slice(&s.0.to_le_bytes());
            }
            for e in &index {
                o.extend_from_slice(&e.to_le_bytes());
            }
        });
        self.keytables.insert(dkey, off);
        Ok(off)
    }

    pub fn dict(&mut self, keytable: u64, values: &[Slot]) -> Result<Slot> {
        let n = check_len(values.len(), "dict")?;
        let off = self.container(NODE_DICT, 0, n, |o| {
            o.extend_from_slice(&keytable.to_le_bytes());
            for s in values {
                o.extend_from_slice(&s.0.to_le_bytes());
            }
        });
        Ok(Slot::reference(Tag::Dict, off))
    }

    pub fn finish(mut self, root: Slot, checksum: bool) -> Vec<u8> {
        let data_size = self.out.len() as u64;
        let checksum_value = if checksum {
            body_checksum(&self.out, data_size)
        } else {
            0
        };
        let header = Header {
            kind: Kind::Ro,
            flags: if checksum { FLAG_CHECKSUM } else { 0 },
            data_size,
            root: root.0,
            checksum: checksum_value,
        };
        header.write(&mut self.out);
        self.out
    }
}

fn norm_key<'a>(k: &KeyIn<'a>) -> Result<Key<'a>> {
    Ok(match *k {
        KeyIn::None => Key::None,
        KeyIn::Bool(b) => Key::Int(IntKey::Small(i128::from(b))),
        KeyIn::Int(v) => Key::Int(IntKey::Small(i128::from(v))),
        KeyIn::BigInt(b) => Key::Int(IntKey::from_le(b)),
        KeyIn::Float(f) => Key::from_f64(f).ok_or_else(|| FormatError::new("NaN не может быть ключом"))?,
        KeyIn::Str { utf8, .. } => Key::Str(utf8.as_bytes().into()),
        KeyIn::Bytes(b) => Key::Bytes(b.into()),
    })
}
