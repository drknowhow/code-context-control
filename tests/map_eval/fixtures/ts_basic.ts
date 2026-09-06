import { EventEmitter } from "events";
import type { Logger } from "./logger";

export interface Job {
  id: string;
  payload: Record<string, unknown>;
  retries?: number;
}

export type JobHandler = (job: Job) => Promise<void>;

export enum JobState {
  Pending = "pending",
  Running = "running",
  Done = "done",
}

export const DEFAULT_CONCURRENCY = 4;

export class Queue extends EventEmitter {
  private jobs: Job[] = [];

  constructor(private readonly log: Logger) {
    super();
  }

  get size(): number {
    return this.jobs.length;
  }

  push(job: Job): void {
    this.jobs.push(job);
    this.emit("push", job);
  }

  async drain(handler: JobHandler): Promise<number> {
    let count = 0;
    while (this.jobs.length) {
      await handler(this.jobs.shift() as Job);
      count += 1;
    }
    this.log.info(`drained ${count}`);
    return count;
  }
}

export const makeQueue = (log: Logger): Queue => new Queue(log);

export default function createJob(id: string, payload: Record<string, unknown>): Job {
  return { id, payload, retries: 0 };
}
