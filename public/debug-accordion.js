/**
 * Chainlit UI enhancements:
 *
 * Strategy: Work WITH Chainlit's React-managed DOM, never against it.
 *
 * 1) Starter prompts — Chainlit renders native #starters via @cl.set_starters.
 *    We enhance those buttons with description text from /starter-prompts API.
 *    All layout/styling is CSS-only (targets #starters, #welcome-screen).
 *    Chainlit handles show/hide lifecycle (empty state only) automatically.
 *
 * 2) Follow-up suggestion chips — click → fill input (manual send).
 *
 * 3) Hide feedback buttons (like/dislike).
 */
(function () {
  "use strict";

  var scheduled = false;
  var starterData = null; // [{label, message}] from /starter-prompts API
  var starterDataLoaded = false;

  // ─── Helpers ───────────────────────────────────────────────

  function getTextarea() {
    return document.querySelector("textarea");
  }

  /**
   * Fill the textarea and update React's controlled-input state.
   *
   * Key: React tracks the "last known value" via an internal
   * _valueTracker on the DOM node.  Resetting it to "" forces
   * React to see a real delta on the subsequent synthetic `input`
   * event, which updates component state (i) and immediately
   * enables the send button (if the socket is connected).
   *
   * This does NOT auto-submit — the user clicks the send arrow.
   */
  function populateInput(text) {
    var textarea = getTextarea();
    if (!textarea) return;
    var nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value"
    ).set;
    nativeSetter.call(textarea, text);
    var tracker = textarea._valueTracker;
    if (tracker) tracker.setValue("");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
    textarea.focus();
  }

  // ─── Load description data from /starter-prompts API ───────

  function loadStarterData() {
    if (starterDataLoaded) return;
    starterDataLoaded = true;

    fetch("/starter-prompts")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || !Array.isArray(data.prompts)) return;
        starterData = data.prompts
          .filter(function (p) {
            if (typeof p === "string") return p.trim().length > 0;
            return p && (p.message || "").trim().length > 0;
          })
          .map(function (p) {
            if (typeof p === "string") return { label: p, message: p };
            return { label: p.label || p.message, message: p.message };
          });
        scheduleApply();
      })
      .catch(function () {
        starterData = null;
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

  // ─── Enhance Chainlit native starter buttons ───────────────
  //
  // Chainlit renders:
  //   <div id="starters" class="flex gap-2 justify-center flex-wrap">
  //     <button id="starter-{slug}">
  //       <div class="flex gap-2">
  //         <p class="text-sm text-muted-foreground truncate">{label}</p>
  //       </div>
  //     </button>
  //   </div>
  //
  // We enhance each button with:
  //   - A title (short label) + description (full message) layout
  //   - Mark as enhanced to avoid re-processing

  function enhanceNativeStarters() {
    var container = document.getElementById("starters");
    if (!container) return;

    // Mark the container for CSS grid styling
    container.classList.add("starter-cards-grid");

    var buttons = container.querySelectorAll('button[id^="starter-"]');
    if (!buttons.length) return;

    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];

      // ── Always remove disabled (runs every MutationObserver pass) ──
      // Chainlit's React sets disabled={loading||!connected} and may
      // re-apply it on re-renders. We strip it every time so the button
      // is always interactive. Our click handler bypasses the form.
      if (btn.hasAttribute("disabled")) {
        btn.removeAttribute("disabled");
      }

      // Skip if already fully enhanced (visual content + click handler).
      // "full" means starterData was available during enhancement.
      // If enhanced without starterData, re-enhance now if data is ready.
      if (btn.dataset.cardEnhanced === "full") continue;
      if (btn.dataset.cardEnhanced === "partial" && !starterData) continue;

      // If re-enhancing (partial → full), remove old injected content
      if (btn.dataset.cardEnhanced === "partial") {
        var oldContent = btn.querySelector(".starter-card-content");
        if (oldContent) oldContent.remove();
        // Restore the hidden inner div so label re-matching works
        var hiddenInner = btn.querySelector("div");
        if (hiddenInner) hiddenInner.style.display = "";
      }

      btn.dataset.cardEnhanced = starterData ? "full" : "partial";

      // Add card class for CSS
      btn.classList.add("starter-card");

      // Find the matching starter data for this button (by label match)
      var labelP = btn.querySelector("p");
      var nativeLabel = labelP ? labelP.textContent.trim() : "";
      var matchedData = findStarterData(nativeLabel);

      // Determine the message to send on click
      var messageToSend = (matchedData ? matchedData.message : null) || nativeLabel;

      // ── Attach click handler (capture phase, once only) ──
      // Fills the textbox with the prompt text. User clicks send manually.
      // stopPropagation prevents Chainlit's handler from interfering.
      if (!btn.dataset.clickBound) {
        btn.dataset.clickBound = "true";
        (function (btnRef) {
          btnRef.addEventListener(
            "click",
            function (e) {
              e.stopPropagation();
              e.preventDefault();
              // Read message from data attribute (may update on re-enhance)
              populateInput(btnRef.dataset.promptMessage || "");
            },
            true // capture phase — fires first
          );
        })(btn);
      }
      btn.dataset.promptMessage = messageToSend;

      // Clear existing inner content (Chainlit's <div><p>label</p></div>)
      var inner = btn.querySelector("div");
      if (inner) inner.style.display = "none";

      // Build card content
      var content = document.createElement("span");
      content.className = "starter-card-content";

      if (matchedData && matchedData.label !== matchedData.message) {
        // Title + description layout
        var titleEl = document.createElement("span");
        titleEl.className = "starter-card-title";
        titleEl.textContent = matchedData.label;
        content.appendChild(titleEl);

        var descEl = document.createElement("span");
        descEl.className = "starter-card-desc";
        descEl.textContent = matchedData.message;
        content.appendChild(descEl);
      } else {
        // Single text layout
        var textEl = document.createElement("span");
        textEl.className = "starter-card-text";
        textEl.textContent = nativeLabel || (matchedData ? matchedData.message : "");
        content.appendChild(textEl);
      }

      btn.appendChild(content);
    }
  }

  function findStarterData(nativeLabel) {
    if (!starterData || !nativeLabel) return null;

    // Match by label (exact or substring)
    var lower = nativeLabel.toLowerCase();
    for (var i = 0; i < starterData.length; i++) {
      if (starterData[i].label.toLowerCase() === lower) return starterData[i];
    }
    // Fuzzy: check if native label starts with our label or vice versa
    for (var j = 0; j < starterData.length; j++) {
      var sl = starterData[j].label.toLowerCase();
      if (lower.indexOf(sl) === 0 || sl.indexOf(lower) === 0) return starterData[j];
    }
    // Fall back to index-based matching
    var allButtons = document.querySelectorAll('#starters button[id^="starter-"]');
    for (var k = 0; k < allButtons.length; k++) {
      var lbl = allButtons[k].querySelector("p");
      if (lbl && lbl.textContent.trim() === nativeLabel && k < starterData.length) {
        return starterData[k];
      }
    }
    return null;
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

      // Hide Chainlit message-level copy action for suggestion blocks
      var step = chip.closest(".step");
      if (step) {
        step.classList.add("suggestion-step");
        var stepButtons = step.querySelectorAll("button");
        for (var b = 0; b < stepButtons.length; b++) {
          stepButtons[b].style.display = "none";
        }
      }

      // Remove the copy icon button inside the chip (if any)
      var copyBtn = chip.querySelector(".suggestion-chip-copy");
      if (copyBtn) copyBtn.remove();

      // Click anywhere on the chip → fill input (user sends manually)
      (function (p) {
        chip.addEventListener("click", function () {
          populateInput(p);
        });
      })(prompt);
    }
  }

  // ─── Main loop ─────────────────────────────────────────────

  function applyUiUpdates() {
    hideFeedbackButtons();
    loadStarterData();
    enhanceNativeStarters();
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
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,            // catch React prop changes (disabled, etc.)
      attributeFilter: ["disabled", "class"], // limit to relevant attributes
    });
    window.addEventListener("resize", scheduleApply);
    scheduleApply();

    // Safari fix: schedule extra enhancement passes during the first few
    // seconds after load.  Catches cases where:
    //   - Chainlit's React renders #starters AFTER our initial pass
    //   - starterData fetch completes after buttons were first enhanced
    //   - Safari delays the empty-state mount due to hydration timing
    var retries = [500, 1000, 2000, 3500];
    retries.forEach(function (ms) {
      setTimeout(scheduleApply, ms);
    });
  }

  // Safari bfcache: when the user navigates back, Safari restores the
  // page from memory without re-executing scripts.  The MutationObserver
  // is still alive but may not fire if React didn't change the DOM.
  // Re-run enhancements on pageshow with persisted=true.
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) {
      starterDataLoaded = false; // re-fetch in case API state changed
      scheduleApply();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
