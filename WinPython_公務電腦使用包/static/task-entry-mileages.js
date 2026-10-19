(function () {
  "use strict";
  window.startVehicleMileageSync = function (onUpdate) {
    let pending = false;
    async function refresh() {
      if (pending || document.visibilityState === "hidden") return;
      pending = true;
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch("/api/vehicle-mileages", {cache: "no-store", signal: controller.signal});
        if (!response.ok) return;
        const data = await response.json();
        if (!data || !data.mileages || !data.times || !data.history) return;
        onUpdate(data);
      } catch (_) {
        // Keep the current reference while NAS is temporarily unavailable.
      } finally {
        window.clearTimeout(timeout);
        pending = false;
      }
    }
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    window.setInterval(refresh, 30000);
    refresh();
  };
}());
