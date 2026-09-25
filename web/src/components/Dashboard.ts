/**
 * Main dashboard component — connects to Python via QWebChannel + WASM telemetry
 */

import { VmConfig, VmMetrics, TelemetrySample } from '../types';

export class Dashboard {
  constructor(
    private root: HTMLElement,
    private bridge: any,
    private bufferSeconds: number = 300
  ) {}

  async initialize(): Promise<void> {
    this.render();
    // WASM telemetry ring buffer initialized here
  }

  private render(): void {
    this.root.innerHTML = '<div class="vmharness-dashboard">VM-Harness Web UI Ready</div>';
  }
}
