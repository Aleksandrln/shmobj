//! Формат shmobj RO v1: чтение, сборка и проверка сегмента поверх `&[u8]`.
//!
//! Нормативное описание формата — `doc/format.md`.
//! Ничего не знает о Python; все ошибки данных — [`FormatError`], без паник.

pub mod format;
pub mod ro;

pub use format::error::{FormatError, Result};
pub use format::hash::{key_hash64, IntKey, Key};
pub use format::header::{Header, Kind};
pub use format::slot::{Slot, Tag};
pub use ro::dict::{DictView, KeyTable};
pub use ro::list::{ListSub, ListView};
pub use ro::reader::{Seg, ValueRef};
pub use ro::validate::validate;
pub use ro::writer::{KeyIn, Writer};

/// Максимальная вложенность контейнеров при сборке и рекурсивных обходах.
pub const MAX_DEPTH: usize = 1000;
