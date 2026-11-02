//! Слоты, хэш ключей против векторов эталона, golden, сборка и поиск.
#![allow(clippy::unwrap_used, clippy::indexing_slicing, clippy::panic)]

use std::borrow::Cow;
use std::path::PathBuf;

use shmobj_core::{key_hash64, validate, IntKey, Key, KeyIn, ListSub, Seg, Slot, ValueRef, Writer};

fn golden(name: &str) -> Vec<u8> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden")
        .join(format!("{name}.bin"));
    std::fs::read(p).unwrap()
}

const GOLDEN: &[&str] = &[
    "scalars",
    "typed_lists",
    "keys",
    "sorted_dict",
    "records",
    "nested",
    "empty_dict",
    "empty_list",
    "root_scalar",
];

#[test]
fn smallint_bounds() {
    for v in [0i64, 1, -1, (1 << 59) - 1, -(1 << 59)] {
        assert_eq!(Slot::small_int(v).unwrap().small_int_value(), v);
    }
    assert!(Slot::small_int(1 << 59).is_none());
    assert!(Slot::small_int(-(1 << 59) - 1).is_none());
}

#[test]
fn shortstr_roundtrip() {
    for s in ["", "a", "1234567", "юни", "😀"] {
        let slot = Slot::short_str(s.as_bytes()).unwrap();
        assert_eq!(slot.short_str_bytes().unwrap().as_slice(), s.as_bytes());
    }
    assert!(Slot::short_str(b"12345678").is_none());
    // Ненулевые неиспользуемые биты — ошибка.
    let bad = Slot(Slot::short_str(b"a").unwrap().0 | (1 << 60));
    assert!(bad.short_str_bytes().is_err());
}

#[test]
fn key_hash_vectors() {
    let int = |v: i128| Key::Int(IntKey::Small(v));
    let cases: Vec<(Key<'_>, u64)> = vec![
        (Key::None, 0xc44bdff4074eecdb),
        (int(0), 0xab94ac0ace7276c0),
        (int(1), 0x070d80d6a76865f3),
        (Key::from_f64(1.0).unwrap(), 0x070d80d6a76865f3),
        (Key::from_f64(-0.0).unwrap(), 0xab94ac0ace7276c0),
        (int(-1), 0x08f88395ec2f5ae4),
        (int(1 << 63), 0x57d8705469819976),
        (int(-(1i128 << 70)), 0xb245c8cfe8835e5b),
        (Key::from_f64(2.5).unwrap(), 0x9ef387e90592c04b),
        (Key::from_f64(f64::INFINITY).unwrap(), 0x7bbac6f0db7f77d1),
        (Key::from_f64(1e300).unwrap(), 0xf5efeb57bc0c9a6f),
        (Key::Str(Cow::Borrowed(b"s")), 0xa7442ad55b7b1f58),
        (Key::Str(Cow::Borrowed("длинный ключ".as_bytes())), 0x1c267b5efb03e3d5),
        (Key::Bytes(Cow::Borrowed(b"b")), 0xd5508b6602842fbf),
    ];
    for (k, h) in cases {
        assert_eq!(key_hash64(&k), h, "{k:?}");
    }
    assert!(Key::from_f64(f64::NAN).is_none());
}

#[test]
fn golden_validate() {
    for name in GOLDEN {
        validate(&golden(name)).unwrap_or_else(|e| panic!("{name}: {e}"));
    }
}

#[test]
fn golden_lookup_sorted_dict() {
    let buf = golden("sorted_dict");
    let (seg, h) = Seg::open(&buf).unwrap();
    let ValueRef::Dict(d) = seg.decode(Slot(h.root)).unwrap() else {
        panic!()
    };
    assert!(d.keys.sorted);
    for i in 0..100 {
        let k = format!("k{i}");
        match d.get(&Key::Str(Cow::Borrowed(k.as_bytes()))).unwrap() {
            Some(ValueRef::Int(v)) => assert_eq!(v, i),
            other => panic!("{k}: {other:?}"),
        }
    }
    assert!(d.get(&Key::Str(Cow::Borrowed(b"k100"))).unwrap().is_none());
}

#[test]
fn golden_numeric_key_equivalence() {
    let buf = golden("keys");
    let (seg, h) = Seg::open(&buf).unwrap();
    let ValueRef::Dict(d) = seg.decode(Slot(h.root)).unwrap() else {
        panic!()
    };
    // {None: 0, True: 1, 2: 2, 2.5: 3, ...}: 1 и 1.0 находят ключ True, 2.0 — ключ 2.
    for (k, want) in [
        (Key::from_f64(1.0).unwrap(), 1),
        (Key::Int(IntKey::Small(2)), 2),
        (Key::from_f64(2.5).unwrap(), 3),
    ] {
        assert!(matches!(d.get(&k).unwrap(), Some(ValueRef::Int(v)) if v == want));
    }
}

#[test]
fn writer_typed_and_dedup() {
    let mut w = Writer::new(true);
    let a = w.str("long string value", 17).unwrap();
    let b = w.str("long string value", 17).unwrap();
    assert_eq!(a, b);
    let ints = w.list_ints(&[0, 300]).unwrap();
    let keys = [
        KeyIn::Str {
            utf8: "x",
            char_count: 1,
        },
        KeyIn::Int(7),
    ];
    let kt1 = w.keytable(&keys).unwrap();
    let kt2 = w.keytable(&keys).unwrap();
    assert_eq!(kt1, kt2);
    let d = w.dict(kt1, &[a, ints]).unwrap();
    let root = w.list_slots(&[d, d]).unwrap();
    let buf = w.finish(root, true);
    validate(&buf).unwrap();
    let (seg, h) = Seg::open(&buf).unwrap();
    let ValueRef::List(l) = seg.decode(Slot(h.root)).unwrap() else {
        panic!()
    };
    let ValueRef::Dict(d) = l.get(1).unwrap() else { panic!() };
    let ValueRef::List(inner) = d.get(&Key::Int(IntKey::Small(7))).unwrap().unwrap() else {
        panic!()
    };
    assert_eq!(inner.sub, ListSub::U16);
    assert!(matches!(inner.get(1).unwrap(), ValueRef::Int(300)));
}

#[test]
fn corruption_never_panics() {
    let buf = golden("records");
    // Без контрольной суммы, чтобы проверялся сам разбор.
    let mut base = buf.clone();
    base[12..16].copy_from_slice(&0u32.to_le_bytes());
    base[32..40].copy_from_slice(&0u64.to_le_bytes());
    validate(&base).unwrap();
    for pos in 64..base.len() {
        for v in [0u8, 1, 0x7F, 0x80, 0xFF] {
            let mut b = base.clone();
            b[pos] ^= v.wrapping_add(1);
            let _ = validate(&b);
        }
    }
}
