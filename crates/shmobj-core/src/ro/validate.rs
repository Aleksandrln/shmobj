//! Полная проверка сегмента (doc/format.md §9). Обход итеративный:
//! глубина вложенности не ограничена стеком.

use std::collections::HashSet;

use crate::format::error::{FormatError, Result};
use crate::format::hash::key_hash64;
use crate::format::header::body_checksum;
use crate::format::slot::{Slot, Tag};

use super::dict::KeyTable;
use super::list::ListSub;
use super::reader::{Seg, ValueRef};

pub fn validate(buf: &[u8]) -> Result<()> {
    let (seg, h) = Seg::open(buf)?;
    if h.has_checksum() && body_checksum(buf, h.data_size) != h.checksum {
        return Err(FormatError::new("контрольная сумма не совпала"));
    }
    let mut seen: HashSet<u64> = HashSet::new();
    let mut keytables_ok: HashSet<u64> = HashSet::new();
    let mut stack: Vec<(Slot, u64)> = vec![(Slot(h.root), h.data_size)];
    while let Some((s, parent)) = stack.pop() {
        let tag = s.tag()?;
        let p = s.payload();
        if tag.is_ref() && p >= parent {
            return Err(FormatError::at("ссылка вперёд (нарушен пост-порядок)", p));
        }
        let v = seg.decode(s)?;
        check_utf8(&v)?;
        if tag.is_ref() && !seen.insert(p) {
            continue;
        }
        match v {
            ValueRef::List(l) if l.sub == ListSub::Slots => {
                for i in 0..l.len() {
                    stack.push((l.slot(i)?, p));
                }
            }
            ValueRef::Dict(d) => {
                if keytables_ok.insert(d.keys.off) {
                    check_keytable(seg, &d.keys)?;
                }
                for i in 0..d.len() {
                    stack.push((d.value_slot(i)?, p));
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn check_utf8(v: &ValueRef<'_>) -> Result<()> {
    let bytes = match v {
        ValueRef::Str { utf8, .. } => *utf8,
        ValueRef::ShortStr(inl) => inl.as_slice(),
        _ => return Ok(()),
    };
    std::str::from_utf8(bytes)
        .map(|_| ())
        .map_err(|_| FormatError::new("строка не UTF-8"))
}

fn check_keytable(seg: Seg<'_>, kt: &KeyTable<'_>) -> Result<()> {
    let n = kt.len as usize;
    let mut keys = Vec::with_capacity(n);
    let mut unique = HashSet::with_capacity(n);
    for i in 0..n {
        let s = kt.key_slot(i)?;
        let tag = s.tag()?;
        if tag.is_ref() && s.payload() >= kt.off {
            return Err(FormatError::at("ссылка вперёд (нарушен пост-порядок)", kt.off));
        }
        if matches!(tag, Tag::List | Tag::Dict) {
            return Err(FormatError::at("контейнер в роли ключа", kt.off));
        }
        check_utf8(&seg.decode(s)?)?;
        let k = seg.decode_key(s)?;
        if !unique.insert(k.clone()) {
            return Err(FormatError::at("повторяющиеся ключи", kt.off));
        }
        keys.push(k);
    }
    if !kt.sorted {
        return Ok(());
    }
    let mut used = vec![false; n];
    let mut prev = 0u64;
    for j in 0..n {
        let e = kt.index_entry(j)?;
        if j > 0 && e < prev {
            return Err(FormatError::at("битый индекс KEYTABLE: не отсортирован", kt.off));
        }
        prev = e;
        let i = (e & 0xFFFF_FFFF) as usize;
        match used.get_mut(i) {
            Some(u) if !*u => *u = true,
            _ => return Err(FormatError::at("битый индекс KEYTABLE: не перестановка", kt.off)),
        }
        let k = keys
            .get(i)
            .ok_or_else(|| FormatError::at("битый индекс KEYTABLE", kt.off))?;
        if key_hash64(k) >> 32 != e >> 32 {
            return Err(FormatError::at("хэш в индексе не совпадает с ключом", kt.off));
        }
    }
    Ok(())
}
