(function () {
  "use strict";

  var scheduled = false;
  var starterData = null;
  var starterDataLoaded = false;

  function getTextarea() {
    return document.querySelector("textarea");
  }

  function ensureLogoutButton() {
    var existing = document.querySelector("#sso-logout-button");
    if (existing) return;

    var button = document.createElement("a");
    button.id = "sso-logout-button";
    button.href = "/chat/auth/logout";
    button.textContent = "Log out";
    button.setAttribute("aria-label", "Log out");

    document.body.appendChild(button);
  }

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

  function enhanceNativeStarters() {
    var container = document.getElementById("starters");
    if (!container) return;

    container.classList.add("starter-cards-grid");

    var buttons = container.querySelectorAll('button[id^="starter-"]');
    if (!buttons.length) return;

    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];

      if (btn.hasAttribute("disabled")) {
        btn.removeAttribute("disabled");
      }

      if (btn.dataset.cardEnhanced === "full") continue;
      if (btn.dataset.cardEnhanced === "partial" && !starterData) continue;

      if (btn.dataset.cardEnhanced === "partial") {
        var oldContent = btn.querySelector(".starter-card-content");
        if (oldContent) oldContent.remove();
        var hiddenInner = btn.querySelector("div");
        if (hiddenInner) hiddenInner.style.display = "";
      }

      btn.dataset.cardEnhanced = starterData ? "full" : "partial";
      btn.classList.add("starter-card");

      var labelP = btn.querySelector("p");
      var nativeLabel = labelP ? labelP.textContent.trim() : "";
      var matchedData = findStarterData(nativeLabel);
      var messageToSend = (matchedData ? matchedData.message : null) || nativeLabel;

      if (!btn.dataset.clickBound) {
        btn.dataset.clickBound = "true";
        (function (btnRef) {
          btnRef.addEventListener(
            "click",
            function (e) {
              e.stopPropagation();
              e.preventDefault();
              populateInput(btnRef.dataset.promptMessage || "");
            },
            true
          );
        })(btn);
      }
      btn.dataset.promptMessage = messageToSend;

      var inner = btn.querySelector("div");
      if (inner) inner.style.display = "none";

      var content = document.createElement("span");
      content.className = "starter-card-content";

      if (matchedData && matchedData.label !== matchedData.message) {
        var titleEl = document.createElement("span");
        titleEl.className = "starter-card-title";
        titleEl.textContent = matchedData.label;
        content.appendChild(titleEl);

        var descEl = document.createElement("span");
        descEl.className = "starter-card-desc";
        descEl.textContent = matchedData.message;
        content.appendChild(descEl);
      } else {
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

    var lower = nativeLabel.toLowerCase();
    for (var i = 0; i < starterData.length; i++) {
      if (starterData[i].label.toLowerCase() === lower) return starterData[i];
    }
    for (var j = 0; j < starterData.length; j++) {
      var sl = starterData[j].label.toLowerCase();
      if (lower.indexOf(sl) === 0 || sl.indexOf(lower) === 0) return starterData[j];
    }
    var allButtons = document.querySelectorAll('#starters button[id^="starter-"]');
    for (var k = 0; k < allButtons.length; k++) {
      var lbl = allButtons[k].querySelector("p");
      if (lbl && lbl.textContent.trim() === nativeLabel && k < starterData.length) {
        return starterData[k];
      }
    }
    return null;
  }

  function enhanceSuggestionChips() {
    var chips = document.querySelectorAll(".suggestion-chip");
    for (var i = 0; i < chips.length; i++) {
      var chip = chips[i];
      if (chip.dataset.enhanced) continue;
      chip.dataset.enhanced = "true";

      var prompt = chip.dataset.prompt;
      if (!prompt) continue;

      var step = chip.closest(".step");
      if (step) {
        step.classList.add("suggestion-step");
        var stepButtons = step.querySelectorAll("button");
        for (var b = 0; b < stepButtons.length; b++) {
          stepButtons[b].style.display = "none";
        }
      }

      var copyBtn = chip.querySelector(".suggestion-chip-copy");
      if (copyBtn) copyBtn.remove();

      (function (p) {
        chip.addEventListener("click", function () {
          populateInput(p);
        });
      })(prompt);
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
    ensureLogoutButton();
    hideFeedbackButtons();
    loadStarterData();
    enhanceNativeStarters();
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
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["disabled", "class"],
    });
    window.addEventListener("resize", scheduleApply);
    scheduleApply();

    var retries = [500, 1000, 2000, 3500];
    retries.forEach(function (ms) {
      setTimeout(scheduleApply, ms);
    });
  }

  window.addEventListener("pageshow", function (e) {
    if (e.persisted) {
      starterDataLoaded = false;
      scheduleApply();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
