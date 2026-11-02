use std::fmt;

/// Нарушение формата: причина и смещение узла, где оно найдено (если известно).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FormatError {
    pub msg: String,
    pub offset: Option<u64>,
}

pub type Result<T> = std::result::Result<T, FormatError>;

impl FormatError {
    pub fn new(msg: impl Into<String>) -> Self {
        Self {
            msg: msg.into(),
            offset: None,
        }
    }

    pub fn at(msg: impl Into<String>, offset: u64) -> Self {
        Self {
            msg: msg.into(),
            offset: Some(offset),
        }
    }
}

impl fmt::Display for FormatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.offset {
            Some(off) => write!(f, "{} (смещение {})", self.msg, off),
            None => f.write_str(&self.msg),
        }
    }
}

impl std::error::Error for FormatError {}
