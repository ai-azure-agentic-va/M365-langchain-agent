/**
 * Chainlit UI enhancements:
 * 1) Starter prompts: click → populate input only (no auto-send)
 * 2) Follow-up suggestions: click → populate input and auto-send
 * 3) Hide feedback buttons (like/dislike)
 */
(function () {
  var scheduled = false;
  var cachedStarterContainer = null;
  var starterPromptsLoaded = false;
  var starterPrompts = [];

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

  function loadStarterPrompts() {
    if (starterPromptsLoaded) return;
    starterPromptsLoaded = true;

    fetch("/starter-prompts")
      .then(function (r) {
        if (!r.ok) throw new Error("starter-prompts fetch failed");
        return r.json();
      })
      .then(function (data) {
        var prompts = data && Array.isArray(data.prompts) ? data.prompts : [];
        starterPrompts = prompts.filter(function (p) {
          return typeof p === "string" && p.trim().length > 0;
        });
        scheduleApply();
      })
      .catch(function () {
        starterPrompts = [];
      });
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

  function getComposerContainer(form) {
    if (!form) return null;
    var node = form;
    while (node && node !== document.body) {
      var style = window.getComputedStyle(node);
      if (style.position === "fixed" || style.position === "sticky") {
        return node;
      }
      node = node.parentElement;
    }
    return form.parentElement || form;
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

  function enhanceCustomStarterChips() {
    var chips = document.querySelectorAll(".starter-chip");
    for (var i = 0; i < chips.length; i++) {
      var chip = chips[i];
      if (chip.dataset.enhanced) continue;
      chip.dataset.enhanced = "true";

      var prompt = chip.dataset.starterPrompt;
      if (!prompt) continue;

      // Click on starter chip text -> populate input only (user edits, then Enter).
      var textSpan = chip.querySelector(".starter-chip-text");
      if (textSpan) {
        (function (p) {
          textSpan.addEventListener("click", function () {
            populateInput(p);
          });
        })(prompt);
      }
    }
  }

  function ensureStarterAnchor(form) {
    if (!form) return null;
    var host = form.parentElement || form;
    var anchor = document.querySelector("#starter-prompts-fixed-tray");
    if (!anchor) {
      anchor = document.createElement("div");
      anchor.id = "starter-prompts-fixed-tray";
      host.appendChild(anchor);
    } else if (anchor.parentElement !== host) {
      host.appendChild(anchor);
    }
    return anchor;
  }

  function renderStarterPromptsBelowInput() {
    var form = getMessageForm();
    if (!form) return;
    var anchor = ensureStarterAnchor(form);
    if (!anchor) return;

    // Ensure no legacy starter blocks remain in message list.
    var legacy = document.querySelectorAll(".starter-chips-container");
    for (var i = 0; i < legacy.length; i++) {
      var step = legacy[i].closest(".step");
      legacy[i].style.display = "none";
      if (step) step.style.display = "none";
    }

    if (!starterPrompts.length) {
      anchor.innerHTML = "";
      anchor.style.display = "none";
      return;
    }

    var host = form.parentElement || form;
    host.style.display = "flex";
    host.style.flexDirection = "column";
    host.style.alignItems = "stretch";
    host.style.gap = "12px";
    host.style.overflow = "visible";

    anchor.style.display = "block";
    anchor.style.position = "relative";
    anchor.style.left = "";
    anchor.style.width = "100%";
    anchor.style.bottom = "";
    anchor.style.zIndex = "";
    anchor.style.order = "2";

    var hash = starterPrompts.join("||");
    var existing = anchor.querySelector(".starter-chips-rendered");
    if (existing && existing.dataset.hash === hash) return;
    if (existing) existing.remove();

    var container = document.createElement("div");
    container.className = "starter-chips-rendered starter-prompts-grid";
    container.dataset.hash = hash;

    for (var j = 0; j < starterPrompts.length; j++) {
      var chip = document.createElement("div");
      chip.className = "suggestion-chip starter-chip";
      chip.dataset.starterPrompt = starterPrompts[j];

      var textSpan = document.createElement("span");
      textSpan.className = "suggestion-chip-text starter-chip-text";
      textSpan.textContent = starterPrompts[j];
      chip.appendChild(textSpan);

      container.appendChild(chip);
    }

    anchor.appendChild(container);
  }

  function getStarterTextsFromDom() {
    var texts = [];
    var seen = {};
    var starterTextEls = document.querySelectorAll(".starter-chip-text");
    for (var i = 0; i < starterTextEls.length; i++) {
      var text = (starterTextEls[i].textContent || "").trim().replace(/\s+/g, " ");
      if (text && !seen[text]) {
        seen[text] = true;
        texts.push(text);
      }
    }
    return texts;
  }

  function buildStarterContainer(texts) {
    var container = document.createElement("div");
    container.className = "suggestion-chips-container starter-chips-container starter-chips-rendered";

    var label = document.createElement("div");
    label.className = "suggestion-chips-label";
    label.textContent = "Try one of these prompts";
    container.appendChild(label);

    for (var i = 0; i < texts.length; i++) {
      var chip = document.createElement("div");
      chip.className = "suggestion-chip starter-chip";
      chip.dataset.starterPrompt = texts[i];

      var textSpan = document.createElement("span");
      textSpan.className = "suggestion-chip-text starter-chip-text";
      textSpan.textContent = texts[i];
      chip.appendChild(textSpan);

      container.appendChild(chip);
    }
    return container;
  }

  function hideOriginalStarterBlocks(anchor) {
    var blocks = document.querySelectorAll(".starter-chips-container");
    for (var i = 0; i < blocks.length; i++) {
      var block = blocks[i];
      if (anchor && anchor.contains(block)) continue;
      block.style.display = "none";
      var step = block.closest(".step");
      if (step) step.style.display = "none";
    }
  }

  function positionStarterChipsBelowInput() {
    // Legacy no-op: starter prompts are now rendered in a dedicated fixed tray.
    var sourceBlocks = document.querySelectorAll(".starter-chips-container");
    for (var i = 0; i < sourceBlocks.length; i++) {
      var step = sourceBlocks[i].closest(".step");
      sourceBlocks[i].style.display = "none";
      if (step) step.style.display = "none";
    }
  }

  // ─── Follow-up suggestion chips ────────────────────────────

  function enhanceSuggestionChips() {
    var chips = document.querySelectorAll(".suggestion-chip");
    for (var i = 0; i < chips.length; i++) {
      var chip = chips[i];
      if (chip.classList.contains("starter-chip")) continue;
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

  // ─── Logout button ─────────────────────────────────────────

  function addLogoutButton() {
    // Check if logout button already exists
    if (document.getElementById("custom-logout-btn")) return;

    // Try multiple selectors to find a suitable container
    var container = document.querySelector("header") ||
                   document.querySelector("nav") ||
                   document.querySelector("[role='banner']") ||
                   document.body.firstElementChild;

    if (!container) {
      console.warn("[Logout] Could not find container for logout button");
      return;
    }

    // Create logout button
    var logoutBtn = document.createElement("button");
    logoutBtn.id = "custom-logout-btn";
    logoutBtn.textContent = "Logout";
    logoutBtn.style.cssText =
      "position: fixed; right: 20px; top: 20px; " +
      "padding: 8px 16px; background: #ef4444; color: white; border: none; " +
      "border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500; " +
      "transition: background 0.15s ease; z-index: 9999; box-shadow: 0 2px 8px rgba(0,0,0,0.15);";

    // Hover effect
    logoutBtn.addEventListener("mouseenter", function () {
      logoutBtn.style.background = "#dc2626";
    });
    logoutBtn.addEventListener("mouseleave", function () {
      logoutBtn.style.background = "#ef4444";
    });

    // Click handler - redirect to logout endpoint
    logoutBtn.addEventListener("click", function () {
      console.log("[Logout] Redirecting to logout...");
      window.location.href = "/auth/logout";
    });

    // Append to body for guaranteed visibility
    document.body.appendChild(logoutBtn);
    console.log("[Logout] Logout button added successfully");
  }

  // ─── Main loop ─────────────────────────────────────────────

  function applyUiUpdates() {
    hideFeedbackButtons();
    loadStarterPrompts();
    enhanceStarterPrompts();
    enhanceCustomStarterChips();
    renderStarterPromptsBelowInput();
    positionStarterChipsBelowInput();
    enhanceSuggestionChips();
    addLogoutButton();
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
    window.addEventListener("resize", scheduleApply);
    scheduleApply();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
