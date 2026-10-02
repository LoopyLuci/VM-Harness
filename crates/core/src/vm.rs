use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VmConfig {
    pub name: String,
    pub memory_mb: u32,
    pub cpus: u16,
    pub disk_path: String,
    pub disk_format: DiskFormat,
    pub iso_path: Option<String>,
    pub network_mode: NetworkMode,
    pub display_type: DisplayType,
    pub qmp_addr: String,
    pub ssh_port: u16,
    pub enable_kvm: bool,
    pub extra_args: Vec<String>,
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum DiskFormat {
    Qcow2,
    Raw,
    Vmdk,
    Vdi,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum NetworkMode {
    Nat,
    Bridge { interface: String },
    User,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum DisplayType {
    Sdl,
    Vnc { port: u16 },
    Spice { port: u16 },
    Gtk,
    None,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum VmState {
    Stopped,
    Booting,
    Running,
    Paused,
    Crashed,
    ShuttingDown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VmMetrics {
    pub cpu_percent: f64,
    pub memory_used_mb: u64,
    pub memory_total_mb: u64,
    pub disk_read_bytes: u64,
    pub disk_write_bytes: u64,
    pub net_rx_bytes: u64,
    pub net_tx_bytes: u64,
    pub uptime_seconds: u64,
    pub timestamp: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VmInfo {
    pub id: String,
    pub name: String,
    pub state: VmState,
    pub config: VmConfig,
    pub metrics: Option<VmMetrics>,
    pub pid: Option<u32>,
    pub started_at: Option<DateTime<Utc>>,
    pub last_stop_reason: Option<String>,
}
