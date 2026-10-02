use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION_MAJOR: u32 = 1;
pub const PROTOCOL_VERSION_MINOR: u32 = 0;
pub const BUILD_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Version {
    pub major: u32,
    pub minor: u32,
    pub patch: u32,
}

impl Version {
    pub fn protocol() -> Self {
        Self {
            major: PROTOCOL_VERSION_MAJOR,
            minor: PROTOCOL_VERSION_MINOR,
            patch: 0,
        }
    }

    pub fn build() -> Self {
        let v = BUILD_VERSION;
        let parts: Vec<u32> = v.split('.').filter_map(|s| s.parse().ok()).collect();
        Self {
            major: parts.first().copied().unwrap_or(0),
            minor: parts.get(1).copied().unwrap_or(0),
            patch: parts.get(2).copied().unwrap_or(0),
        }
    }

    pub fn compatible(&self, other: &Self) -> bool {
        self.major == other.major
    }
}

impl std::fmt::Display for Version {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}.{}.{}", self.major, self.minor, self.patch)
    }
}
