//! `FrozenList` — `collections.abc.Sequence` поверх узла LIST.
//! Срез — представление `(start, step, len)` над тем же узлом, без копирования.

use pyo3::exceptions::{PyIndexError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PySlice};
use shmobj_core::{ListView, Seg};

use crate::decode::{materialize, to_py};
use crate::errors::{format_err, pickle_err};
use crate::iter::ListIter;
use crate::segment::Segment;
use crate::Opt;

#[pyclass(frozen, sequence, module = "shmobj")]
pub struct FrozenList {
    seg: Py<Segment>,
    off: u64,
    start: i64,
    step: i64,
    len: usize,
}

impl FrozenList {
    pub fn new(seg: Py<Segment>, off: u64, len: u32) -> FrozenList {
        FrozenList {
            seg,
            off,
            start: 0,
            step: 1,
            len: len as usize,
        }
    }

    fn with_view<'py, R>(
        slf: &Bound<'py, Self>,
        f: impl FnOnce(&Bound<'py, Segment>, Seg<'_>, ListView<'_>) -> PyResult<R>,
    ) -> PyResult<R> {
        let me = slf.get();
        let seg_obj = me.seg.bind(slf.py());
        seg_obj.get().with_seg(|seg| {
            let view = ListView::open(seg, me.off).map_err(format_err)?;
            f(seg_obj, seg, view)
        })
    }

    /// Индекс в узле для логического индекса `j < len`.
    pub fn raw_index(&self, j: usize) -> usize {
        (self.start + j as i64 * self.step) as usize
    }

    fn normalize(&self, i: isize) -> Option<usize> {
        let n = self.len as isize;
        let i = if i < 0 { i + n } else { i };
        (0..n).contains(&i).then_some(i as usize)
    }

    fn item<'py>(slf: &Bound<'py, Self>, j: usize) -> PyResult<Bound<'py, PyAny>> {
        let raw = slf.get().raw_index(j);
        Self::with_view(slf, |seg_obj, _, view| {
            to_py(slf.py(), seg_obj, view.get(raw).map_err(format_err)?)
        })
    }

    fn iter_range(slf: &Bound<'_, Self>, reversed: bool) -> PyResult<ListIter> {
        let me = slf.get();
        me.seg.get().check_open()?;
        let (start, step) = if reversed && me.len > 0 {
            (me.start + (me.len as i64 - 1) * me.step, -me.step)
        } else {
            (me.start, me.step)
        };
        Ok(ListIter::new(me.seg.clone_ref(slf.py()), me.off, start, step, me.len))
    }

    /// Первый логический индекс в `[lo, hi)`, где элемент `== value`.
    fn find<'py>(slf: &Bound<'py, Self>, value: &Bound<'py, PyAny>, lo: usize, hi: usize) -> PyResult<Option<usize>> {
        for j in lo..hi.min(slf.get().len) {
            if Self::item(slf, j)?.eq(value)? {
                return Ok(Some(j));
            }
        }
        Ok(None)
    }
}

fn clamp_index(i: isize, len: usize) -> usize {
    let n = len as isize;
    let i = if i < 0 { (i + n).max(0) } else { i.min(n) };
    i as usize
}

#[pymethods]
impl FrozenList {
    fn __len__(&self) -> PyResult<usize> {
        self.seg.get().check_open()?;
        Ok(self.len)
    }

    fn __getitem__<'py>(slf: &Bound<'py, Self>, index: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let me = slf.get();
        if let Ok(s) = index.cast::<PySlice>() {
            me.seg.get().check_open()?;
            let ind = s.indices(me.len as isize)?;
            let view = FrozenList {
                seg: me.seg.clone_ref(slf.py()),
                off: me.off,
                start: me.start + ind.start as i64 * me.step,
                step: me.step * ind.step as i64,
                len: ind.slicelength,
            };
            return Ok(Bound::new(slf.py(), view)?.into_any());
        }
        let i: isize = index
            .extract()
            .map_err(|_| PyTypeError::new_err("индексы FrozenList должны быть int или slice"))?;
        let j = me
            .normalize(i)
            .ok_or_else(|| PyIndexError::new_err("индекс FrozenList вне диапазона"))?;
        Self::item(slf, j)
    }

    fn __iter__(slf: &Bound<'_, Self>) -> PyResult<ListIter> {
        Self::iter_range(slf, false)
    }

    fn __reversed__(slf: &Bound<'_, Self>) -> PyResult<ListIter> {
        Self::iter_range(slf, true)
    }

    fn __contains__<'py>(slf: &Bound<'py, Self>, value: &Bound<'py, PyAny>) -> PyResult<bool> {
        slf.get().seg.get().check_open()?;
        Ok(Self::find(slf, value, 0, slf.get().len)?.is_some())
    }

    #[pyo3(signature = (value, start = 0, stop = isize::MAX))]
    fn index<'py>(slf: &Bound<'py, Self>, value: &Bound<'py, PyAny>, start: isize, stop: isize) -> PyResult<usize> {
        let len = slf.get().len;
        slf.get().seg.get().check_open()?;
        Self::find(slf, value, clamp_index(start, len), clamp_index(stop, len))?
            .ok_or_else(|| PyValueError::new_err("значение не найдено в FrozenList"))
    }

    fn count<'py>(slf: &Bound<'py, Self>, value: &Bound<'py, PyAny>) -> PyResult<usize> {
        slf.get().seg.get().check_open()?;
        let mut n = 0;
        for j in 0..slf.get().len {
            if Self::item(slf, j)?.eq(value)? {
                n += 1;
            }
        }
        Ok(n)
    }

    /// Значения по индексам. Без `default` выход за границы даёт `IndexError`.
    #[pyo3(signature = (indices, default = Opt(None)))]
    fn get_many<'py>(
        slf: &Bound<'py, Self>,
        indices: &Bound<'py, PyAny>,
        default: Opt<'py>,
    ) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(slf.py());
        for i in indices.try_iter()? {
            let i: isize = i?.extract()?;
            match slf.get().normalize(i) {
                Some(j) => out.append(Self::item(slf, j)?)?,
                None => match &default.0 {
                    Some(d) => out.append(d)?,
                    None => return Err(PyIndexError::new_err("индекс FrozenList вне диапазона")),
                },
            }
        }
        Ok(out)
    }

    /// То же, что `iter(l)`.
    fn iter_values(slf: &Bound<'_, Self>) -> PyResult<ListIter> {
        Self::iter_range(slf, false)
    }

    /// Глубокая копия в обычный `list`.
    fn materialize<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        let me = slf.get();
        Self::with_view(slf, |seg_obj, _, view| {
            let out = PyList::empty(slf.py());
            for j in 0..me.len {
                let v = view.get(me.raw_index(j)).map_err(format_err)?;
                out.append(materialize(slf.py(), seg_obj, v, 1)?)?;
            }
            Ok(out.into_any())
        })
    }

    /// Равен `list` или `FrozenList` с равными элементами.
    fn __eq__<'py>(slf: &Bound<'py, Self>, other: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        if !(other.cast::<PyList>().is_ok() || other.cast::<FrozenList>().is_ok()) {
            return Ok(py.NotImplemented());
        }
        let len = slf.get().__len__()?;
        let mut equal = other.len()? == len;
        if equal {
            for (j, ov) in other.try_iter()?.enumerate() {
                if j >= len || !Self::item(slf, j)?.eq(ov?)? {
                    equal = false;
                    break;
                }
            }
        }
        Ok(equal.into_pyobject(py)?.to_owned().into_any().unbind())
    }

    fn __repr__(&self) -> String {
        format!("<FrozenList len={}>", self.len)
    }

    fn __reduce_ex__(&self, _protocol: &Bound<'_, PyAny>) -> PyResult<()> {
        Err(pickle_err("FrozenList"))
    }
}
