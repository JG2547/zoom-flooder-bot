// Counts consecutive identical messages (exact match). When the run exceeds
// `threshold`, fires a single random reply, marks the burst handled, and starts
// a cooldown. A different message text resets the run.
//
// Message-id dedup means re-emitted/replayed messages on rejoin won't double-count.
export class ChatSpamDetector {
  constructor({
    threshold = 10,
    cooldownSec = 90,
    responses = [],
    onSpam,
    persisted = {},
  } = {}) {
    this.threshold = threshold;
    this.cooldownMs = cooldownSec * 1000;
    this.responses = responses;
    this.onSpam = onSpam;

    this.lastText = persisted.lastText ?? null;
    this.consecutiveCount = persisted.consecutiveCount ?? 0;
    this.responded = persisted.responded ?? false;
    this.cooldownUntil = persisted.cooldownUntil ?? 0;
    this.offenders = new Map(persisted.offenders ?? []);
    this.processedIds = new Set(persisted.processedIds ?? []);
  }

  serialize() {
    // Cap the processedIds set so localStorage doesn't grow unbounded.
    const ids = Array.from(this.processedIds).slice(-500);
    return {
      lastText: this.lastText,
      consecutiveCount: this.consecutiveCount,
      responded: this.responded,
      cooldownUntil: this.cooldownUntil,
      offenders: Array.from(this.offenders.entries()),
      processedIds: ids,
    };
  }

  observe({ id, sender, text }) {
    if (!text) return null;
    if (id != null) {
      if (this.processedIds.has(id)) return null;
      this.processedIds.add(id);
    }

    if (text === this.lastText) {
      this.consecutiveCount += 1;
    } else {
      this.lastText = text;
      this.consecutiveCount = 1;
      this.responded = false;
    }

    const now = Date.now();
    if (
      this.consecutiveCount > this.threshold &&
      !this.responded &&
      now >= this.cooldownUntil
    ) {
      this.responded = true;
      this.cooldownUntil = now + this.cooldownMs;
      const strikes = (this.offenders.get(sender) || 0) + 1;
      this.offenders.set(sender, strikes);
      const reply = this.responses.length
        ? this.responses[Math.floor(Math.random() * this.responses.length)]
        : null;
      const event = {
        sender,
        text,
        count: this.consecutiveCount,
        strikes,
        reply,
        ts: new Date().toISOString(),
      };
      this.onSpam?.(event);
      return event;
    }
    return null;
  }
}
