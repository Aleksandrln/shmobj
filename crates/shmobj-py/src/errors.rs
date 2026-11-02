use pyo3::create_exception;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::PyErr;

create_exception!(shmobj, FormatError, PyValueError, "Нарушение формата сегмента.");
create_exception!(
    shmobj,
    SegmentClosedError,
    PyValueError,
    "Обращение к закрытому сегменту."
);

pub fn format_err(e: shmobj_core::FormatError) -> PyErr {
    FormatError::new_err(e.to_string())
}

pub fn closed_err() -> PyErr {
    SegmentClosedError::new_err("сегмент закрыт")
}

pub fn pickle_err(what: &str) -> PyErr {
    PyTypeError::new_err(format!(
        "{what} не сериализуется pickle: используйте materialize() или передайте имя сегмента"
    ))
}
