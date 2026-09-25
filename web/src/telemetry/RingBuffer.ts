/**
 * Telemetry Ring Buffer — TypeScript mirror of Rust WASM module
 */

export class RingBuffer<T> {
  private buffer: T[] = [];
  private head = 0;
  private count = 0;

  constructor(private readonly capacity: number) {}

  push(item: T): void {
    if (this.buffer.length < this.capacity) {
      this.buffer.push(item);
    } else {
      this.buffer[this.head] = item;
    }
    this.head = (this.head + 1) % this.capacity;
    this.count++;
  }

  getValues(): T[] {
    if (this.count < this.capacity) return this.buffer.slice(0, this.count);
    return [...this.buffer.slice(this.head), ...this.buffer.slice(0, this.head)];
  }

  clear(): void {
    this.buffer = [];
    this.head = 0;
    this.count = 0;
  }
}
