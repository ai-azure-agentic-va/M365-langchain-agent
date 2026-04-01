/**
 * Lightweight UI helpers for Chainlit:
 * 1) Keep native accordion rendering server-side
 * 2) Remove message feedback buttons
 */
(function () {
  var scheduled = false;
  var cachedStarterContainer = null;

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

  function isVisible(el) {
    return !!(el && el.offsetParent !== null);
  }

  function getMessageForm() {
    var textarea = document.querySelector("textarea");
    if (!textarea) {
      return null;
    }
    return textarea.closest("form");
  }

  function isStarterButton(btn) {
    if (!isVisible(btn) || btn.closest("form") || btn.id.indexOf("action-") === 0) {
      return false;
    }
    var text = (btn.textContent || "").trim().replace(/\s+/g, " ");
    return text.length >= 18 && text.length <= 220;
  }

  function getStarterCandidateContainer(form) {
    if (!form || !form.parentElement) {
      return null;
    }

    if (cachedStarterContainer && document.body.contains(cachedStarterContainer)) {
      return cachedStarterContainer;
    }

    var candidates = form.parentElement.querySelectorAll("div, section");
    var best = null;
    var bestScore = 0;

    for (var i = 0; i < candidates.length; i++) {
      var container = candidates[i];
      if (container === form || container.contains(form)) {
        continue;
      }

      var buttons = container.querySelectorAll("button:not([id^='action-'])");
      if (buttons.length < 3) {
        continue;
      }

      var score = 0;
      for (var j = 0; j < buttons.length; j++) {
        var button = buttons[j];
        if (isStarterButton(button)) {
          score += 1;
        }
      }

      if (score >= 2 && score > bestScore) {
        best = container;
        bestScore = score;
      }
    }

    cachedStarterContainer = best;
    return best;
  }

  function enhanceStarterPrompts() {
    var form = getMessageForm();
    if (!form || !form.parentElement) {
      return;
    }

    var starterContainer = getStarterCandidateContainer(form);
    if (!starterContainer) {
      return;
    }

    starterContainer.classList.add("cl-starter-prompts-container");
    var promptButtons = starterContainer.querySelectorAll("button:not([id^='action-'])");
    for (var i = 0; i < promptButtons.length; i++) {
      var promptButton = promptButtons[i];
      if (isStarterButton(promptButton)) {
        promptButton.classList.add("cl-starter-prompt-card");
      }
    }

    var parent = form.parentElement;
    if (starterContainer.parentElement === parent && form.nextSibling !== starterContainer) {
      parent.insertBefore(starterContainer, form.nextSibling);
    }
  }

  function applyUiUpdates() {
    hideFeedbackButtons();
    enhanceStarterPrompts();
  }

  function scheduleApply() {
    if (scheduled) {
      return;
    }
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      applyUiUpdates();
    });
  }

  var observer = new MutationObserver(scheduleApply);

  function init() {
    if (!document.body) {
      setTimeout(init, 250);
      return;
    }
    observer.observe(document.body, { childList: true, subtree: true });
    scheduleApply();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
