(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  root.QuoteRefreshController = exported.QuoteRefreshController;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  class QuoteRefreshController {
    constructor(options) {
      this.fetcher = options.fetcher;
      this.onData = options.onData || (() => {});
      this.onError = options.onError || (() => {});
      this.schedule = options.schedule || ((fn, delay) => setTimeout(fn, delay));
      this.cancelSchedule = options.cancelSchedule || ((id) => clearTimeout(id));
      this.timeoutMs = options.timeoutMs || 4000;
      this.backoff = options.backoff || [5000, 10000, 20000, 40000, 60000];
      this.failureCount = 0;
      this.sequence = 0;
      this.timer = null;
      this.controller = null;
      this.visible = true;
    }

    async refresh() {
      const sequence = ++this.sequence;
      if (this.controller) this.controller.abort();
      const controller = new AbortController();
      this.controller = controller;
      const timeout = this.schedule(() => controller.abort(), this.timeoutMs);
      try {
        const value = await this.fetcher(controller.signal, sequence);
        if (sequence !== this.sequence) return;
        this.failureCount = 0;
        this.onData(value);
        this._queue(this.backoff[0]);
      } catch (error) {
        if (sequence !== this.sequence) return;
        this.failureCount += 1;
        this.onError(error);
        this._queue(this.backoff[Math.min(this.failureCount, this.backoff.length - 1)]);
      } finally {
        this.cancelSchedule(timeout);
        if (this.controller === controller) this.controller = null;
      }
    }

    setVisible(visible) {
      this.visible = Boolean(visible);
      if (!this.visible) {
        if (this.timer) this.cancelSchedule(this.timer);
        this.timer = null;
        if (this.controller) this.controller.abort();
        return;
      }
      this.refresh();
    }

    stop() {
      this.sequence += 1;
      if (this.timer) this.cancelSchedule(this.timer);
      if (this.controller) this.controller.abort();
      this.timer = null;
      this.controller = null;
    }

    _queue(delay) {
      if (this.timer) this.cancelSchedule(this.timer);
      if (!this.visible) return;
      this.timer = this.schedule(() => this.refresh(), delay);
    }
  }

  return { QuoteRefreshController };
});
