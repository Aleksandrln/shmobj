//! Слот -> Python-объект.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyString};
use shmobj_core::{ValueRef, MAX_DEPTH};

use crate::errors::format_err;
use crate::frozen_dict::FrozenDict;
use crate::frozen_list::FrozenList;
use crate::segment::Segment;

fn int_from_le<'py>(py: Python<'py>, b: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    static FROM_BYTES: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    let f = FROM_BYTES.get_or_try_init(py, || -> PyResult<_> {
        Ok(py.get_type::<pyo3::types::PyInt>().getattr("from_bytes")?.unbind())
    })?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("signed", true)?;
    f.bind(py).call((PyBytes::new(py, b), "little"), Some(&kwargs))
}

/// Скаляр или новый прокси контейнера.
pub fn to_py<'py>(py: Python<'py>, seg: &Bound<'py, Segment>, v: ValueRef<'_>) -> PyResult<Bound<'py, PyAny>> {
    Ok(match v {
        ValueRef::None => py.None().into_bound(py),
        ValueRef::Bool(b) => PyBool::new(py, b).to_owned().into_any(),
        ValueRef::Int(i) => i.into_pyobject(py)?.into_any(),
        ValueRef::BigInt(b) => int_from_le(py, b)?,
        ValueRef::Float(f) => PyFloat::new(py, f).into_any(),
        ValueRef::Str { utf8, .. } => PyString::from_bytes(py, utf8)?.into_any(),
        ValueRef::ShortStr(inl) => PyString::from_bytes(py, inl.as_slice())?.into_any(),
        ValueRef::Bytes(b) => PyBytes::new(py, b).into_any(),
        ValueRef::List(l) => Bound::new(py, FrozenList::new(seg.clone().unbind(), l.off, l.len))?.into_any(),
        ValueRef::Dict(d) => Bound::new(py, FrozenDict::new(seg.clone().unbind(), d.off, d.len))?.into_any(),
    })
}

/// Глубокое преобразование в `dict`/`list`.
pub fn materialize<'py>(
    py: Python<'py>,
    seg_obj: &Bound<'py, Segment>,
    v: ValueRef<'_>,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    if depth > MAX_DEPTH {
        return Err(PyValueError::new_err("слишком глубокая вложенность"));
    }
    Ok(match v {
        ValueRef::List(l) => {
            let out = PyList::empty(py);
            for i in 0..l.len() {
                out.append(materialize(py, seg_obj, l.get(i).map_err(format_err)?, depth + 1)?)?;
            }
            out.into_any()
        }
        ValueRef::Dict(d) => {
            let out = PyDict::new(py);
            for i in 0..d.len() {
                let k = to_py(py, seg_obj, d.key(i).map_err(format_err)?)?;
                let val = materialize(py, seg_obj, d.value(i).map_err(format_err)?, depth + 1)?;
                out.set_item(k, val)?;
            }
            out.into_any()
        }
        other => to_py(py, seg_obj, other)?,
    })
}
