const KEY = 'zoom-sdk-bot-v1';

export const stateStore = {
  load() {
    try {
      const raw = localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  },
  save(state) {
    try {
      localStorage.setItem(KEY, JSON.stringify(state));
    } catch (err) {
      console.warn('[state] save failed:', err);
    }
  },
  patch(updates) {
    const next = { ...this.load(), ...updates };
    this.save(next);
    return next;
  },
  clear() {
    localStorage.removeItem(KEY);
  },
};
