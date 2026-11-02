//! Модуль `shmobj._core`: прокси поверх сегмента и сборка из Python-объектов.

mod build;
mod decode;
mod errors;
mod frozen_dict;
mod frozen_list;
mod iter;
mod keyarg;
mod segment;

use pyo3::prelude::*;
use pyo3::types::PyBytes;

use errors::{format_err, FormatError, SegmentClosedError};
use frozen_dict::FrozenDict;
use frozen_list::FrozenList;
use segment::Segment;

/// Необязательный аргумент, где явный `None` отличается от отсутствия аргумента.
pub struct Opt<'py>(pub Option<Bound<'py, PyAny>>);

impl<'a, 'py> FromPyObject<'a, 'py> for Opt<'py> {
    type Error = PyErr;

    fn extract(obj: pyo3::Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        Ok(Opt(Some(obj.to_owned())))
    }
}

/// Собрать сегмент в `bytes`.
#[pyfunction]
#[pyo3(signature = (obj, *, dedup = true, checksum = true))]
fn dumps<'py>(obj: &Bound<'py, PyAny>, dedup: bool, checksum: bool) -> PyResult<Bound<'py, PyBytes>> {
    let mut b = build::Builder::new(dedup);
    let root = b.slot(obj)?;
    Ok(PyBytes::new(obj.py(), &b.finish(root, checksum)))
}

/// Полная проверка формата; при любом нарушении — `FormatError`.
#[pyfunction]
fn validate(buf: &Bound<'_, PyAny>) -> PyResult<()> {
    let seg = Segment::open(buf, None, None, None, false)?;
    seg.with_seg(|s| {
        let bytes = s.bytes_all();
        shmobj_core::validate(bytes).map_err(format_err)
    })
}

/// `Segment` поверх буфера. `on_close` вызывается после освобождения буфера, `on_unlink` — в `unlink()`.
#[pyfunction]
#[pyo3(signature = (buf, *, name = None, on_close = None, on_unlink = None, verify = false))]
fn open_segment(
    buf: &Bound<'_, PyAny>,
    name: Option<String>,
    on_close: Option<Py<PyAny>>,
    on_unlink: Option<Py<PyAny>>,
    verify: bool,
) -> PyResult<Segment> {
    Segment::open(buf, name, on_close, on_unlink, verify)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("FORMAT_VERSION", shmobj_core::format::header::FORMAT_VERSION)?;
    m.add("FormatError", py.get_type::<FormatError>())?;
    m.add("SegmentClosedError", py.get_type::<SegmentClosedError>())?;
    m.add_class::<Segment>()?;
    m.add_class::<FrozenDict>()?;
    m.add_class::<FrozenList>()?;
    m.add_function(wrap_pyfunction!(dumps, m)?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    m.add_function(wrap_pyfunction!(open_segment, m)?)?;
    Ok(())
}
