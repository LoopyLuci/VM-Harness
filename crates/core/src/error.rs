use std::fmt;

#[derive(Debug, thiserror::Error)]
pub enum VmError {
    #[error("VM not found: {0}")]
    VmNotFound(String),

    #[error("VM already exists: {0}")]
    VmAlreadyExists(String),

    #[error("Invalid state: {0}")]
    InvalidState(String),

    #[error("QMP error: {0}")]
    QmpError(String),

    #[error("IO error: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Serialization error: {0}")]
    SerializationError(String),
}

pub type Result<T> = std::result::Result<T, VmError>;

impl VmError {
    pub fn is_recoverable(&self) -> bool {
        matches!(self, VmError::QmpError(_) | VmError::IoError(_))
    }
}

impl From<serde_json::Error> for VmError {
    fn from(e: serde_json::Error) -> Self {
        VmError::SerializationError(e.to_string())
    }
}
