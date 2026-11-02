//! Ключ запроса `PyAny` -> нормализованный `Key`.

use std::borrow::Cow;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyString};
use shmobj_core::{IntKey, Key};

/// Байты целого по правилу `int.to_bytes((bit_length + 8) // 8, "little", signed=True)`.
pub fn int_le_bytes(v: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let bits: usize = v.call_method0("bit_length")?.extract()?;
    let kwargs = PyDict::new(v.py());
    kwargs.set_item("signed", true)?;
    let b = v.call_method("to_bytes", ((bits + 8) / 8, "little"), Some(&kwargs))?;
    Ok(b.cast::<PyBytes>()?.as_bytes().to_vec())
}

/// `None` — ключ хэшируемый, но такого типа в таблице быть не может (поиск даёт `KeyError`).
/// Нехэшируемый ключ даёт `TypeError`, как у `dict`.
pub fn to_key<'a>(obj: &'a Bound<'_, PyAny>) -> PyResult<Option<Key<'a>>> {
    if let Ok(s) = obj.cast::<PyString>() {
        return Ok(s.to_str().ok().map(|s| Key::Str(Cow::Borrowed(s.as_bytes()))));
    }
    if obj.is_none() {
        return Ok(Some(Key::None));
    }
    if let Ok(b) = obj.cast::<PyBool>() {
        return Ok(Some(Key::Int(IntKey::Small(i128::from(b.is_true())))));
    }
    if obj.cast::<PyInt>().is_ok() {
        if let Ok(v) = obj.extract::<i64>() {
            return Ok(Some(Key::Int(IntKey::Small(i128::from(v)))));
        }
        let b = int_le_bytes(obj)?;
        return Ok(Some(Key::Int(IntKey::from_le(&b).into_owned())));
    }
    if let Ok(f) = obj.cast::<PyFloat>() {
        return Ok(Key::from_f64(f.value()));
    }
    if let Ok(b) = obj.cast::<PyBytes>() {
        return Ok(Some(Key::Bytes(Cow::Borrowed(b.as_bytes()))));
    }
    obj.hash()?;
    Ok(None)
}
