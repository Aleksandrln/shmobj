//! `Segment`: экспорт буфера (`PyBuffer`) и флаг закрытия.
//!
//! Буфер освобождается в `close()`; если в этот момент идёт обращение к данным
//! (например, сборщик мусора вызвал `close()` из финализатора посреди `get_many`),
//! освобождение откладывается до конца обращения.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use shmobj_core::format::header::body_checksum;
use shmobj_core::Seg;

use crate::decode::to_py;
use crate::errors::{closed_err, format_err, pickle_err, FormatError};

struct Inner {
    buffer: Option<PyBuffer<u8>>,
    on_close: Option<Py<PyAny>>,
    on_unlink: Option<Py<PyAny>>,
}

/// Сегмент данных shmobj: `SharedMemory`, `mmap` или `bytes`.
#[pyclass(frozen, module = "shmobj")]
pub struct Segment {
    ptr: usize,
    data_size: usize,
    root: u64,
    name: Option<String>,
    closed: AtomicBool,
    active: AtomicUsize,
    inner: Mutex<Inner>,
}

struct Active<'a>(&'a Segment);

impl Drop for Active<'_> {
    fn drop(&mut self) {
        if self.0.active.fetch_sub(1, Ordering::AcqRel) == 1 && self.0.closed.load(Ordering::Acquire) {
            Python::attach(|py| {
                if let Err(e) = self.0.release(py) {
                    e.write_unraisable(py, None);
                }
            });
        }
    }
}

impl Segment {
    pub fn open(
        obj: &Bound<'_, PyAny>,
        name: Option<String>,
        on_close: Option<Py<PyAny>>,
        on_unlink: Option<Py<PyAny>>,
        verify: bool,
    ) -> PyResult<Segment> {
        let buffer = PyBuffer::<u8>::get(obj)?;
        if !buffer.is_c_contiguous() {
            return Err(PyValueError::new_err("буфер должен быть C-непрерывным"));
        }
        let ptr = buffer.buf_ptr() as usize;
        let len = buffer.len_bytes();
        // SAFETY: буфер экспортирован и удерживается `buffer` до конца функции;
        // RO-сегмент после публикации не изменяется (контракт doc/spec/04-architecture.md §3).
        let bytes: &[u8] = if len == 0 {
            &[]
        } else {
            unsafe { std::slice::from_raw_parts(ptr as *const u8, len) }
        };
        let (_, header) = Seg::open(bytes).map_err(format_err)?;
        if verify && header.has_checksum() && body_checksum(bytes, header.data_size) != header.checksum {
            return Err(FormatError::new_err("контрольная сумма не совпала"));
        }
        Ok(Segment {
            ptr,
            data_size: header.data_size as usize,
            root: header.root,
            name,
            closed: AtomicBool::new(false),
            active: AtomicUsize::new(0),
            inner: Mutex::new(Inner {
                buffer: Some(buffer),
                on_close,
                on_unlink,
            }),
        })
    }

    /// Доступ к данным на время вызова `f`.
    pub fn with_seg<R>(&self, f: impl FnOnce(Seg<'_>) -> PyResult<R>) -> PyResult<R> {
        if self.closed.load(Ordering::Acquire) {
            return Err(closed_err());
        }
        self.active.fetch_add(1, Ordering::AcqRel);
        let _active = Active(self);
        // SAFETY: пока `active > 0`, `release` не отпускает PyBuffer, поэтому память
        // `[ptr, ptr + data_size)` остаётся экспортированной; `data_size` ≤ длины буфера
        // проверено в `open`. Срез не выходит за пределы `f`.
        let bytes: &[u8] = if self.data_size == 0 {
            &[]
        } else {
            unsafe { std::slice::from_raw_parts(self.ptr as *const u8, self.data_size) }
        };
        f(Seg::from_checked(bytes))
    }

    pub fn check_open(&self) -> PyResult<()> {
        if self.closed.load(Ordering::Acquire) {
            return Err(closed_err());
        }
        Ok(())
    }

    fn release(&self, py: Python<'_>) -> PyResult<()> {
        let (buffer, on_close) = {
            let mut inner = self
                .inner
                .lock()
                .map_err(|_| PyValueError::new_err("сегмент повреждён"))?;
            (inner.buffer.take(), inner.on_close.take())
        };
        if let Some(b) = buffer {
            drop(b);
        }
        if let Some(cb) = on_close {
            cb.call0(py)?;
        }
        Ok(())
    }
}

#[pymethods]
impl Segment {
    /// Корневое значение; при каждом обращении создаётся новый прокси.
    #[getter]
    fn root(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let me = slf.get();
        me.with_seg(|seg| {
            let v = seg.decode(shmobj_core::Slot(me.root)).map_err(format_err)?;
            Ok(to_py(slf.py(), slf, v)?.unbind())
        })
    }

    /// Имя `SharedMemory`; `None` для произвольного буфера.
    #[getter]
    fn name(&self) -> Option<String> {
        self.name.clone()
    }

    /// `data_size` из заголовка.
    #[getter]
    fn size(&self) -> usize {
        self.data_size
    }

    #[getter]
    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Закрывает сегмент в этом процессе. Идемпотентен.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        if self.closed.swap(true, Ordering::AcqRel) {
            return Ok(());
        }
        if self.active.load(Ordering::Acquire) == 0 {
            self.release(py)?;
        }
        Ok(())
    }

    /// Удаляет именованный сегмент из ОС; для буфера без имени ничего не делает.
    fn unlink(&self, py: Python<'_>) -> PyResult<()> {
        let cb = {
            let inner = self
                .inner
                .lock()
                .map_err(|_| PyValueError::new_err("сегмент повреждён"))?;
            inner.on_unlink.as_ref().map(|c| c.clone_ref(py))
        };
        if let Some(cb) = cb {
            cb.call0(py)?;
        }
        Ok(())
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (*_exc))]
    fn __exit__(&self, py: Python<'_>, _exc: &Bound<'_, pyo3::types::PyTuple>) -> PyResult<()> {
        self.close(py)
    }

    fn __repr__(&self) -> String {
        let state = if self.closed() { " closed" } else { "" };
        match &self.name {
            Some(n) => format!("<Segment name={n:?} size={}{state}>", self.data_size),
            None => format!("<Segment size={}{state}>", self.data_size),
        }
    }

    fn __reduce_ex__(&self, _protocol: &Bound<'_, PyAny>) -> PyResult<()> {
        Err(pickle_err("Segment"))
    }
}
