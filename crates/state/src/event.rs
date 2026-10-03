use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use vmharness_core::VmId;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum Event {
    VmCreated {
        vm_id: VmId,
        name: String,
        timestamp: DateTime<Utc>,
    },
    VmStartRequested {
        vm_id: VmId,
        request_id: String,
        timestamp: DateTime<Utc>,
    },
    VmStarted {
        vm_id: VmId,
        pid: u32,
        timestamp: DateTime<Utc>,
    },
    VmStopped {
        vm_id: VmId,
        reason: StopReason,
        timestamp: DateTime<Utc>,
    },
    VmCrashed {
        vm_id: VmId,
        exit_code: i32,
        stderr: String,
        timestamp: DateTime<Utc>,
    },
    VmSnapshotted {
        vm_id: VmId,
        snapshot_name: String,
        timestamp: DateTime<Utc>,
    },
    VmRestored {
        vm_id: VmId,
        snapshot_name: String,
        timestamp: DateTime<Utc>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum StopReason {
    Graceful,
    Forced,
    Shutdown,
}

impl Event {
    pub fn vm_id(&self) -> &VmId {
        match self {
            Event::VmCreated { vm_id, .. } => vm_id,
            Event::VmStartRequested { vm_id, .. } => vm_id,
            Event::VmStarted { vm_id, .. } => vm_id,
            Event::VmStopped { vm_id, .. } => vm_id,
            Event::VmCrashed { vm_id, .. } => vm_id,
            Event::VmSnapshotted { vm_id, .. } => vm_id,
            Event::VmRestored { vm_id, .. } => vm_id,
        }
    }

    pub fn discriminant(&self) -> u8 {
        match self {
            Event::VmCreated { .. } => 1,
            Event::VmStartRequested { .. } => 2,
            Event::VmStarted { .. } => 3,
            Event::VmStopped { .. } => 4,
            Event::VmCrashed { .. } => 5,
            Event::VmSnapshotted { .. } => 6,
            Event::VmRestored { .. } => 7,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VmRuntimeState {
    pub vm_id: VmId,
    pub state: vmharness_core::VmState,
    pub config: vmharness_core::VmConfig,
    pub pid: Option<u32>,
    pub started_at: Option<DateTime<Utc>>,
    pub last_stop_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StateSnapshot {
    pub sequence: u64,
    pub timestamp: DateTime<Utc>,
    pub vms: Vec<VmRuntimeState>,
}
