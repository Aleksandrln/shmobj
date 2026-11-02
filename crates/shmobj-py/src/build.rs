//! Обход Python-объекта -> `Writer`. Путь к элементу строится только при ошибке.

use std::collections::HashSet;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyList, PyMemoryView, PyString, PyTuple};
use shmobj_core::ro::TYPED_MIN_LEN;
use shmobj_core::{KeyIn, Slot, Writer, MAX_DEPTH};

use crate::keyarg::int_le_bytes;

enum PathElem<'py> {
    Index(usize),
    Key(Bound<'py, PyAny>),
}

pub struct Builder<'py> {
    w: Writer,
    active: HashSet<usize>,
    path: Vec<PathElem<'py>>,
}

fn limit_err(e: shmobj_core::FormatError) -> PyErr {
    PyValueError::new_err(e.msg)
}

/// Ключ словаря на время сборки одной KEYTABLE.
enum KeyTmp<'a> {
    In(KeyIn<'a>),
    Big(Vec<u8>),
}

impl<'py> Builder<'py> {
    pub fn new(dedup: bool) -> Builder<'py> {
        Builder {
            w: Writer::new(dedup),
            active: HashSet::new(),
            path: Vec::new(),
        }
    }

    pub fn finish(self, root: Slot, checksum: bool) -> Vec<u8> {
        self.w.finish(root, checksum)
    }

    fn path_str(&self) -> PyResult<String> {
        let mut s = String::from("root");
        for e in &self.path {
            match e {
                PathElem::Index(i) => s.push_str(&format!("[{i}]")),
                PathElem::Key(k) => s.push_str(&format!("[{}]", k.repr()?)),
            }
        }
        Ok(s)
    }

    fn type_err(&self, msg: String) -> PyErr {
        match self.path_str() {
            Ok(p) => PyTypeError::new_err(format!("{p}: {msg}")),
            Err(e) => e,
        }
    }

    fn value_err(&self, msg: &str) -> PyErr {
        match self.path_str() {
            Ok(p) => PyValueError::new_err(format!("{p}: {msg}")),
            Err(e) => e,
        }
    }

    pub fn slot(&mut self, v: &Bound<'py, PyAny>) -> PyResult<Slot> {
        if v.is_none() {
            return Ok(Slot::NONE);
        }
        if let Ok(b) = v.cast_exact::<PyBool>() {
            return Ok(Slot::bool(b.is_true()));
        }
        if v.is_exact_instance_of::<PyInt>() {
            return match v.extract::<i64>() {
                Ok(i) => Ok(self.w.int(i)),
                Err(_) => {
                    let b = int_le_bytes(v)?;
                    self.w.bigint(&b).map_err(|e| self.value_err(&e.msg))
                }
            };
        }
        if let Ok(f) = v.cast_exact::<PyFloat>() {
            return Ok(self.w.float(f.value()));
        }
        if let Ok(s) = v.cast_exact::<PyString>() {
            let utf8 = s.to_str()?;
            let chars = s.len()?;
            return self.w.str(utf8, chars).map_err(|e| self.value_err(&e.msg));
        }
        if let Ok(b) = v.cast_exact::<PyBytes>() {
            return Ok(self.w.bytes(b.as_bytes()));
        }
        if let Ok(b) = v.cast_exact::<PyByteArray>() {
            return Ok(self.w.bytes(&b.to_vec()));
        }
        if v.is_exact_instance_of::<PyMemoryView>() {
            let b = v.py().get_type::<PyBytes>().call1((v,))?;
            return Ok(self.w.bytes(b.cast::<PyBytes>()?.as_bytes()));
        }
        if v.is_exact_instance_of::<PyList>() || v.is_exact_instance_of::<PyTuple>() {
            return self.container(v, Self::list);
        }
        if v.is_exact_instance_of::<PyDict>() {
            return self.container(v, Self::dict);
        }
        let name = v.get_type().name()?;
        Err(self.type_err(format!("неподдерживаемый тип {name}")))
    }

    fn container(
        &mut self,
        v: &Bound<'py, PyAny>,
        body: fn(&mut Self, &Bound<'py, PyAny>) -> PyResult<Slot>,
    ) -> PyResult<Slot> {
        let id = v.as_ptr() as usize;
        if self.active.contains(&id) {
            return Err(self.value_err("циклическая ссылка"));
        }
        if self.active.len() >= MAX_DEPTH {
            return Err(self.value_err("слишком глубокая вложенность"));
        }
        if v.len()? as u64 > shmobj_core::ro::MAX_LEN {
            return Err(self.value_err("больше 2^32-1 элементов"));
        }
        self.active.insert(id);
        let r = body(self, v);
        self.active.remove(&id);
        r
    }

    fn list(&mut self, v: &Bound<'py, PyAny>) -> PyResult<Slot> {
        let items: Vec<Bound<'py, PyAny>> = match v.cast::<PyList>() {
            Ok(l) => l.iter().collect(),
            Err(_) => v.cast::<PyTuple>()?.iter().collect(),
        };
        if items.len() >= TYPED_MIN_LEN {
            if let Some(slot) = self.typed_list(&items)? {
                return Ok(slot);
            }
        }
        let mut slots = Vec::with_capacity(items.len());
        for (i, x) in items.iter().enumerate() {
            self.path.push(PathElem::Index(i));
            let s = self.slot(x)?;
            self.path.pop();
            slots.push(s);
        }
        self.w.list_slots(&slots).map_err(limit_err)
    }

    /// Однородный список одного точного типа `bool`/`float`/`int` (в пределах i64).
    fn typed_list(&mut self, items: &[Bound<'py, PyAny>]) -> PyResult<Option<Slot>> {
        let Some(first) = items.first() else { return Ok(None) };
        let t0 = first.get_type();
        if items.iter().any(|x| !x.get_type().is(&t0)) {
            return Ok(None);
        }
        let r = if first.is_exact_instance_of::<PyBool>() {
            let v: Vec<bool> = items.iter().map(|x| x.is_truthy()).collect::<PyResult<_>>()?;
            self.w.list_bools(&v)
        } else if first.is_exact_instance_of::<PyFloat>() {
            let v: Vec<f64> = items.iter().map(|x| x.extract::<f64>()).collect::<PyResult<_>>()?;
            self.w.list_floats(&v)
        } else if first.is_exact_instance_of::<PyInt>() {
            let Ok(v) = items.iter().map(|x| x.extract::<i64>()).collect::<PyResult<Vec<i64>>>() else {
                return Ok(None);
            };
            self.w.list_ints(&v)
        } else {
            return Ok(None);
        };
        r.map(Some).map_err(limit_err)
    }

    fn dict(&mut self, v: &Bound<'py, PyAny>) -> PyResult<Slot> {
        let d = v.cast::<PyDict>()?;
        let pairs: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = d.iter().collect();
        let tmp = pairs
            .iter()
            .map(|(k, _)| self.key_tmp(k))
            .collect::<PyResult<Vec<_>>>()?;
        let keys: Vec<KeyIn<'_>> = tmp
            .iter()
            .map(|t| match t {
                KeyTmp::In(k) => *k,
                KeyTmp::Big(b) => KeyIn::BigInt(b),
            })
            .collect();
        let kt = self.w.keytable(&keys).map_err(limit_err)?;
        let mut vals = Vec::with_capacity(pairs.len());
        for (k, x) in &pairs {
            self.path.push(PathElem::Key(k.clone()));
            let s = self.slot(x)?;
            self.path.pop();
            vals.push(s);
        }
        self.w.dict(kt, &vals).map_err(limit_err)
    }

    fn key_tmp<'a>(&self, k: &'a Bound<'py, PyAny>) -> PyResult<KeyTmp<'a>> {
        if k.is_none() {
            return Ok(KeyTmp::In(KeyIn::None));
        }
        if let Ok(b) = k.cast_exact::<PyBool>() {
            return Ok(KeyTmp::In(KeyIn::Bool(b.is_true())));
        }
        if k.is_exact_instance_of::<PyInt>() {
            return Ok(match k.extract::<i64>() {
                Ok(i) => KeyTmp::In(KeyIn::Int(i)),
                Err(_) => KeyTmp::Big(int_le_bytes(k)?),
            });
        }
        if let Ok(f) = k.cast_exact::<PyFloat>() {
            if !f.value().is_nan() {
                return Ok(KeyTmp::In(KeyIn::Float(f.value())));
            }
        }
        if let Ok(s) = k.cast_exact::<PyString>() {
            return Ok(KeyTmp::In(KeyIn::Str {
                utf8: s.to_str()?,
                char_count: s.len()?,
            }));
        }
        if let Ok(b) = k.cast_exact::<PyBytes>() {
            return Ok(KeyTmp::In(KeyIn::Bytes(b.as_bytes())));
        }
        let name = k.get_type().name()?;
        Err(self.type_err(format!("неподдерживаемый тип ключа {name}")))
    }
}
