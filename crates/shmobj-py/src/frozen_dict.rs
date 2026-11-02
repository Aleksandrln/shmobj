//! `FrozenDict` — `collections.abc.Mapping` поверх узла DICT.

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyList, PyType};
use shmobj_core::{DictView, Seg};

use crate::decode::{materialize, to_py};
use crate::errors::{format_err, pickle_err};
use crate::iter::{DictIter, IterMode};
use crate::keyarg::to_key;
use crate::segment::Segment;
use crate::Opt;

#[pyclass(frozen, mapping, module = "shmobj")]
pub struct FrozenDict {
    seg: Py<Segment>,
    off: u64,
    len: u32,
}

fn abc<'py>(py: Python<'py>, cell: &'static PyOnceLock<Py<PyType>>, name: &str) -> PyResult<Bound<'py, PyType>> {
    cell.import(py, "collections.abc", name).cloned()
}

impl FrozenDict {
    pub fn new(seg: Py<Segment>, off: u64, len: u32) -> FrozenDict {
        FrozenDict { seg, off, len }
    }

    fn with_view<'py, R>(
        slf: &Bound<'py, Self>,
        f: impl FnOnce(&Bound<'py, Segment>, Seg<'_>, DictView<'_>) -> PyResult<R>,
    ) -> PyResult<R> {
        let me = slf.get();
        let seg_obj = me.seg.bind(slf.py());
        seg_obj.get().with_seg(|seg| {
            let view = DictView::open(seg, me.off).map_err(format_err)?;
            f(seg_obj, seg, view)
        })
    }

    /// Значение по ключу; `Ok(None)` — ключа нет.
    fn lookup<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let Some(k) = to_key(key)? else {
            slf.get().seg.get().check_open()?;
            return Ok(None);
        };
        Self::with_view(slf, |seg_obj, _, view| match view.get(&k).map_err(format_err)? {
            Some(v) => to_py(slf.py(), seg_obj, v).map(Some),
            None => Ok(None),
        })
    }
}

#[pymethods]
impl FrozenDict {
    fn __len__(&self) -> PyResult<usize> {
        self.seg.get().check_open()?;
        Ok(self.len as usize)
    }

    fn __getitem__<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        Self::lookup(slf, key)?.ok_or_else(|| PyKeyError::new_err(key.clone().unbind()))
    }

    #[pyo3(signature = (key, default = None))]
    fn get<'py>(
        slf: &Bound<'py, Self>,
        key: &Bound<'py, PyAny>,
        default: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        Ok(match Self::lookup(slf, key)? {
            Some(v) => v,
            None => default.unwrap_or_else(|| slf.py().None().into_bound(slf.py())),
        })
    }

    fn __contains__<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyAny>) -> PyResult<bool> {
        let Some(k) = to_key(key)? else {
            slf.get().seg.get().check_open()?;
            return Ok(false);
        };
        Self::with_view(slf, |_, _, view| Ok(view.keys.find(&k).map_err(format_err)?.is_some()))
    }

    fn __iter__(&self, py: Python<'_>) -> PyResult<DictIter> {
        self.seg.get().check_open()?;
        Ok(DictIter::new(
            self.seg.clone_ref(py),
            self.off,
            self.len,
            IterMode::Keys,
        ))
    }

    /// Представление ключей без копирования.
    fn keys<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        static T: PyOnceLock<Py<PyType>> = PyOnceLock::new();
        abc(slf.py(), &T, "KeysView")?.call1((slf,))
    }

    /// Представление значений без копирования.
    fn values<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        static T: PyOnceLock<Py<PyType>> = PyOnceLock::new();
        abc(slf.py(), &T, "ValuesView")?.call1((slf,))
    }

    /// Представление пар без копирования.
    fn items<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        static T: PyOnceLock<Py<PyType>> = PyOnceLock::new();
        abc(slf.py(), &T, "ItemsView")?.call1((slf,))
    }

    /// Значения для `keys` в их порядке. Без `default` отсутствующий ключ даёт `KeyError`.
    #[pyo3(signature = (keys, default = Opt(None)))]
    fn get_many<'py>(
        slf: &Bound<'py, Self>,
        keys: &Bound<'py, PyAny>,
        default: Opt<'py>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = slf.py();
        let out = PyList::empty(py);
        for key in keys.try_iter()? {
            let key = key?;
            match Self::lookup(slf, &key)? {
                Some(v) => out.append(v)?,
                None => match &default.0 {
                    Some(d) => out.append(d)?,
                    None => return Err(PyKeyError::new_err(key.unbind())),
                },
            }
        }
        Ok(out)
    }

    /// Итератор значений в порядке вставки.
    fn iter_values(&self, py: Python<'_>) -> PyResult<DictIter> {
        self.seg.get().check_open()?;
        Ok(DictIter::new(
            self.seg.clone_ref(py),
            self.off,
            self.len,
            IterMode::Values,
        ))
    }

    /// Итератор пар `(ключ, значение)`.
    fn iter_items(&self, py: Python<'_>) -> PyResult<DictIter> {
        self.seg.get().check_open()?;
        Ok(DictIter::new(
            self.seg.clone_ref(py),
            self.off,
            self.len,
            IterMode::Items,
        ))
    }

    /// Глубокая копия в обычный `dict`.
    fn materialize<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        Self::with_view(slf, |seg_obj, _, view| {
            materialize(slf.py(), seg_obj, shmobj_core::ValueRef::Dict(view), 0)
        })
    }

    /// Равен любому `Mapping` с теми же парами.
    fn __eq__<'py>(slf: &Bound<'py, Self>, other: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        static MAPPING: PyOnceLock<Py<PyType>> = PyOnceLock::new();
        let py = slf.py();
        if !other.is_instance(abc(py, &MAPPING, "Mapping")?.as_any())? {
            return Ok(py.NotImplemented());
        }
        if other.is(slf) {
            return Ok(true.into_pyobject(py)?.to_owned().into_any().unbind());
        }
        if other.len()? != slf.get().__len__()? {
            return Ok(false.into_pyobject(py)?.to_owned().into_any().unbind());
        }
        let items = DictIter::new(
            slf.get().seg.clone_ref(py),
            slf.get().off,
            slf.get().len,
            IterMode::Items,
        );
        let items = Bound::new(py, items)?;
        let mut equal = true;
        while let Some(pair) = DictIter::next_item(&items)? {
            let (k, v) = pair.extract::<(Bound<'py, PyAny>, Bound<'py, PyAny>)>()?;
            let ov = match other.get_item(&k) {
                Ok(ov) => ov,
                Err(e) if e.is_instance_of::<PyKeyError>(py) => {
                    equal = false;
                    break;
                }
                Err(e) => return Err(e),
            };
            if !v.eq(ov)? {
                equal = false;
                break;
            }
        }
        Ok(equal.into_pyobject(py)?.to_owned().into_any().unbind())
    }

    fn __repr__(&self) -> String {
        format!("<FrozenDict len={}>", self.len)
    }

    fn __reduce_ex__(&self, _protocol: &Bound<'_, PyAny>) -> PyResult<()> {
        Err(pickle_err("FrozenDict"))
    }
}
