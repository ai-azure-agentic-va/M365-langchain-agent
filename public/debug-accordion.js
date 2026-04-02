/**
 * Chainlit UI enhancements:
 * 1) Starter prompts: click → populate input only (no auto-send)
 * 2) Follow-up suggestions: click → populate input and auto-send
 * 3) Hide feedback buttons (like/dislike)
 */
(function () {
  var scheduled = false;
  var cachedStarterContainer = null;

  // ─── Helpers ───────────────────────────────────────────────

  function getTextarea() {
    return document.querySelector("textarea");
  }

  function populateInput(text) {
    var textarea = getTextarea();
    if (!textarea) return;

    // Set value using React-compatible native setter
    var nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value"
    ).set;
    nativeSetter.call(textarea, text);

    // Dispatch input event so React picks up the change
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.focus();
  }

  function submitCurrentInput() {
    var textarea = getTextarea();
    if (!textarea) return;
    var form = textarea.closest("form");
    if (!form) return;
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
      return;
    }
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }

  function askPrompt(text) {
    populateInput(text);
    setTimeout(function () {
      submitCurrentInput();
    }, 0);
  }

  // ─── Feedback button hiding ────────────────────────────────

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

  // ─── Starter prompt interception ───────────────────────────

  function isVisible(el) {
    return !!(el && el.offsetParent !== null);
  }

  function getMessageForm() {
    var textarea = getTextarea();
    if (!textarea) return null;
    return textarea.closest("form");
  }

  function extractButtonPrompt(btn) {
    if (!btn) return "";
    var text = (btn.textContent || "").trim().replace(/\s+/g, " ");
    if (text) return text;
    var aria = (btn.getAttribute("aria-label") || "").trim();
    if (aria) return aria;
    return (btn.getAttribute("title") || "").trim();
  }

  function isStarterButton(btn) {
    if (!isVisible(btn) || btn.closest("form") || btn.id.indexOf("action-") === 0) {
      return false;
    }
    // Starter cards are plain-text prompt buttons (no action-* ids, not inside the message form).
    // Keep this intentionally permissive so we never miss a starter and trigger auto-send.
    var text = extractButtonPrompt(btn);
    return text.length >= 3;
  }

  function getStarterCandidateContainer(form) {
    if (!form || !form.parentElement) return null;

    if (cachedStarterContainer && document.body.contains(cachedStarterContainer)) {
      return cachedStarterContainer;
    }

    var candidates = form.parentElement.querySelectorAll("div, section");
    var best = null;
    var bestScore = 0;

    for (var i = 0; i < candidates.length; i++) {
      var container = candidates[i];
      if (container === form || container.contains(form)) continue;

      var buttons = container.querySelectorAll("button:not([id^='action-'])");
      if (buttons.length < 3) continue;

      var score = 0;
      for (var j = 0; j < buttons.length; j++) {
        if (isStarterButton(buttons[j])) score += 1;
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
    if (!form || !form.parentElement) return;

    var starterContainer = getStarterCandidateContainer(form);
    if (!starterContainer) return;

    starterContainer.classList.add("cl-starter-prompts-container");
    var promptButtons = starterContainer.querySelectorAll("button:not([id^='action-'])");

    for (var i = 0; i < promptButtons.length; i++) {
      var promptButton = promptButtons[i];
      if (isStarterButton(promptButton)) {
        promptButton.classList.add("cl-starter-prompt-card");

        // Intercept starter click and force prompt -> input only.
        if (!promptButton.dataset.intercepted) {
          promptButton.dataset.intercepted = "true";
          promptButton.dataset.starterPrompt = extractButtonPrompt(promptButton);
          promptButton.addEventListener(
            "pointerdown",
            function (e) {
              e.stopPropagation();
              e.preventDefault();
            },
            true
          );
          promptButton.addEventListener(
            "mousedown",
            function (e) {
              e.stopPropagation();
              e.preventDefault();
            },
            true
          );
          promptButton.addEventListener(
            "click",
            function (e) {
              e.stopPropagation();
              e.preventDefault();
              var text = this.dataset.starterPrompt || extractButtonPrompt(this);
              populateInput(text);
            },
            true // capture phase to beat React's handler
          );
        }
      }
    }

    var parent = form.parentElement;
    if (starterContainer.parentElement === parent && form.nextSibling !== starterContainer) {
      parent.insertBefore(starterContainer, form.nextSibling);
    }
  }

  // ─── Follow-up suggestion chips ────────────────────────────

  function enhanceSuggestionChips() {
    var chips = document.querySelectorAll(".suggestion-chip");
    for (var i = 0; i < chips.length; i++) {
      var chip = chips[i];
      if (chip.dataset.enhanced) continue;
      chip.dataset.enhanced = "true";

      var prompt = chip.dataset.prompt;
      if (!prompt) continue;

      // Hide Chainlit message-level copy action for this suggestion block.
      var step = chip.closest(".step");
      if (step) {
        step.classList.add("suggestion-step");
        var stepButtons = step.querySelectorAll("button");
        for (var b = 0; b < stepButtons.length; b++) {
          stepButtons[b].style.display = "none";
        }
      }

      // Click on the chip text -> populate input and send immediately.
      var textSpan = chip.querySelector(".suggestion-chip-text");
      if (textSpan) {
        (function (p) {
          textSpan.addEventListener("click", function () {
            askPrompt(p);
          });
        })(prompt);
      }
    }
  }

  // ─── Main loop ─────────────────────────────────────────────

  function applyUiUpdates() {
    hideFeedbackButtons();
    enhanceStarterPrompts();
    enhanceSuggestionChips();
  }

  function scheduleApply() {
    if (scheduled) return;
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
