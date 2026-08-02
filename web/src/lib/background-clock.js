/* Logical animation clock kept alive by a Web Worker when the tab is hidden. */

export function createBackgroundClock(intervalMs = 50) {
  const interval = Math.max(16, Math.min(1000, Number(intervalMs) || 50));
  let logicalMs = performance.now();
  let wallMs = Date.now();
  let worker = null;
  let timer = null;
  let objectUrl = "";
  let running = false;

  function advance(nextWallMs) {
    const delta = Math.max(0, Math.min(24 * 60 * 60 * 1000, Number(nextWallMs) - wallMs));
    logicalMs += delta;
    wallMs = Number(nextWallMs);
  }

  function startFallback() {
    if (timer != null) return;
    timer = window.setInterval(() => advance(Date.now()), interval);
  }

  function start() {
    if (running) return;
    running = true;
    wallMs = Date.now();
    try {
      const source = `const delay=${JSON.stringify(interval)};setInterval(()=>postMessage(Date.now()),delay);`;
      objectUrl = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
      worker = new Worker(objectUrl);
      worker.onmessage = (event) => advance(event.data);
      worker.onerror = () => {
        if (worker) worker.terminate();
        worker = null;
        startFallback();
      };
    } catch (_) {
      startFallback();
    }
  }

  function stop() {
    running = false;
    if (worker) worker.terminate();
    if (timer != null) window.clearInterval(timer);
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    worker = null;
    timer = null;
    objectUrl = "";
  }

  function nowSeconds() {
    advance(Date.now());
    return logicalMs / 1000;
  }

  return { start, stop, nowSeconds };
}
