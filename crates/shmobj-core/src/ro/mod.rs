//! RO v1: KEYTABLE, типизированные списки, пост-порядок. RW сюда не зависит.

pub mod dict;
pub mod list;
pub mod reader;
pub mod validate;
pub mod writer;

/// Типы контейнерных узлов (doc/spec/03-format.md §4).
pub const NODE_LIST: u8 = 1;
pub const NODE_DICT: u8 = 2;
pub const NODE_KEYTABLE: u8 = 3;

/// KEYTABLE с `len` больше порога получает индекс SORTED32.
pub const LINEAR_MAX: usize = 8;
/// Минимальная длина типизированного списка.
pub const TYPED_MIN_LEN: usize = 2;
pub const MAX_LEN: u64 = u32::MAX as u64;
