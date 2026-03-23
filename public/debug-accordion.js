/**
 * Lightweight UI helpers for Chainlit:
 * 1) Keep native accordion rendering server-side
 * 2) Remove message feedback buttons
 */
(function () {
  function hideFeedbackButtons() {
    var buttons = document.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var label = (btn.getAttribute("aria-label") || "").toLowerCase();
      var title = (btn.getAttribute("title") || "").toLowerCase();

      if (
        label.indexOf("like") !== -1 ||
        label.indexOf("dislike") !== -1 ||
        label.indexOf("thumb") !== -1 ||
        label.indexOf("feedback") !== -1 ||
        title.indexOf("like") !== -1 ||
        title.indexOf("dislike") !== -1 ||
        title.indexOf("thumb") !== -1 ||
        title.indexOf("feedback") !== -1
      ) {
        btn.style.display = "none";
      }
    }
  }

  function applyUiUpdates() {
    hideFeedbackButtons();
  }

  var observer = new MutationObserver(function () {
    requestAnimationFrame(applyUiUpdates);
  });

  function init() {
    if (!document.body) {
      setTimeout(init, 250);
      return;
    }
    observer.observe(document.body, { childList: true, subtree: true });
    applyUiUpdates();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
