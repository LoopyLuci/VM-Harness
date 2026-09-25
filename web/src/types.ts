/**
 * VM-Harness Core Types — mirrors Python VmConfig exactly
 */

export type VmState = 'stopped' | 'booting' | 'running' | 'paused' | 'crashed' | 'shutting_down';
export type DisplayMode = 'sdl' | 'vnc' | 'gtk' | 'none';

export interface VmConfig {
  readonly name: string;
  readonly memory_mb: number;
  readonly cpus: number;
  readonly disk_path: string;
  readonly qmp_addr: string;
  readonly ssh_port: number;
  readonly use_kvm: boolean;
  readonly display: DisplayMode;
}

export interface VmMetrics {
  readonly cpu_percent: number;
  readonly memory_used_mb: number;
  readonly memory_total_mb: number;
  readonly disk_read_bytes: number;
  readonly disk_write_bytes: number;
  readonly net_rx_bytes: number;
  readonly net_tx_bytes: number;
  readonly uptime_seconds: number;
}
