//! Итераторы `iter_values` / `iter_items` / ключей и элементов списка.

use std::sync::atomic::{AtomicUsize, Ordering};

use pyo3::prelude::*;
use pyo3::types::PyTuple;
use shmobj_core::{DictView, ListView};

use crate::decode::to_py;
use crate::errors::format_err;
use crate::segment::Segment;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum IterMode {
    Keys,
    Values,
    Items,
}

#[pyclass(frozen, module = "shmobj")]
pub struct DictIter {
    seg: Py<Segment>,
    off: u64,
    len: u32,
    pos: AtomicUsize,
    mode: IterMode,
}

impl DictIter {
    pub fn new(seg: Py<Segment>, off: u64, len: u32, mode: IterMode) -> DictIter {
        DictIter {
            seg,
            off,
            len,
            pos: AtomicUsize::new(0),
            mode,
        }
    }

    pub fn next_item<'py>(slf: &Bound<'py, Self>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let me = slf.get();
        let i = me.pos.load(Ordering::Relaxed);
        if i >= me.len as usize {
            return Ok(None);
        }
        let py = slf.py();
        let seg_obj = me.seg.bind(py);
        let out = seg_obj.get().with_seg(|seg| {
            let d = DictView::open(seg, me.off).map_err(format_err)?;
            Ok(match me.mode {
                IterMode::Keys => to_py(py, seg_obj, d.key(i).map_err(format_err)?)?,
                IterMode::Values => to_py(py, seg_obj, d.value(i).map_err(format_err)?)?,
                IterMode::Items => {
                    let k = to_py(py, seg_obj, d.key(i).map_err(format_err)?)?;
                    let v = to_py(py, seg_obj, d.value(i).map_err(format_err)?)?;
                    PyTuple::new(py, [k, v])?.into_any()
                }
            })
        })?;
        me.pos.store(i + 1, Ordering::Relaxed);
        Ok(Some(out))
    }
}

#[pymethods]
impl DictIter {
    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__<'py>(slf: &Bound<'py, Self>) -> PyResult<Option<Bound<'py, PyAny>>> {
        Self::next_item(slf)
    }

    fn __length_hint__(&self) -> usize {
        (self.len as usize).saturating_sub(self.pos.load(Ordering::Relaxed))
    }
}

#[pyclass(frozen, module = "shmobj")]
pub struct ListIter {
    seg: Py<Segment>,
    off: u64,
    start: i64,
    step: i64,
    len: usize,
    pos: AtomicUsize,
}

impl ListIter {
    pub fn new(seg: Py<Segment>, off: u64, start: i64, step: i64, len: usize) -> ListIter {
        ListIter {
            seg,
            off,
            start,
            step,
            len,
            pos: AtomicUsize::new(0),
        }
    }
}

#[pymethods]
impl ListIter {
    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__<'py>(slf: &Bound<'py, Self>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let me = slf.get();
        let j = me.pos.load(Ordering::Relaxed);
        if j >= me.len {
            return Ok(None);
        }
        let py = slf.py();
        let seg_obj = me.seg.bind(py);
        let raw = (me.start + j as i64 * me.step) as usize;
        let out = seg_obj.get().with_seg(|seg| {
            let l = ListView::open(seg, me.off).map_err(format_err)?;
            to_py(py, seg_obj, l.get(raw).map_err(format_err)?)
        })?;
        me.pos.store(j + 1, Ordering::Relaxed);
        Ok(Some(out))
    }

    fn __length_hint__(&self) -> usize {
        self.len.saturating_sub(self.pos.load(Ordering::Relaxed))
    }
}
