// Exponential-backoff reconnect with a hard ceiling and explicit abort.
// Steps cap at the last delay value, so attempts beyond delays.length use that.
const DEFAULT_DELAYS = [5, 15, 30, 60, 120, 300]; // seconds

export class ReconnectManager {
  constructor({
    onAttempt,
    delays = DEFAULT_DELAYS,
    maxAttempts = 12,
    onLog = () => {},
  } = {}) {
    this.onAttempt = onAttempt;
    this.delays = delays;
    this.maxAttempts = maxAttempts;
    this.log = onLog;
    this._reset();
  }

  _reset() {
    this.attempt = 0;
    this.timer = null;
    this.giveUp = false;
    this.giveUpReason = null;
  }

  schedule(reason = '') {
    if (this.giveUp) return;
    if (this.timer) return; // already pending
    if (this.attempt >= this.maxAttempts) {
      this.giveUp = true;
      this.giveUpReason = `max-attempts(${this.maxAttempts})`;
      this.log(`reconnect give-up: ${this.giveUpReason}`);
      return;
    }
    const idx = Math.min(this.attempt, this.delays.length - 1);
    const delaySec = this.delays[idx];
    this.attempt += 1;
    this.log(`reconnect attempt #${this.attempt} in ${delaySec}s (${reason})`);
    this.timer = setTimeout(async () => {
      this.timer = null;
      try {
        await this.onAttempt(this.attempt);
      } catch (err) {
        this.log(`reconnect #${this.attempt} threw: ${err?.message || err}`);
        this.schedule('rejoin-error');
      }
    }, delaySec * 1000);
  }

  reset() {
    if (this.timer) clearTimeout(this.timer);
    this._reset();
  }

  abort(reason) {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.giveUp = true;
    this.giveUpReason = reason;
    this.log(`reconnect aborted: ${reason}`);
  }
}
