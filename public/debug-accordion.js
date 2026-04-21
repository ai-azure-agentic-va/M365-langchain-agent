(function () {
  "use strict";

  var scheduled = false;
  var starterData = null;
  var starterDataLoaded = false;
  var ssoEnabled = null;
  var ssoStatusChecked = false;

  // Sidebar refresh: Chainlit's frontend does not refetch the thread list after
  // first_interaction. The only reload we perform is an explicit one when the
  // user clicks the "New Chat" button (#new-chat-button). Reload is intercepted
  // in capture phase so it happens BEFORE Chainlit's own click handler starts
  // a new session — the fresh page load then shows the updated sidebar.
  var SB_RELOAD_KEY = "sb_reload_last";
  var SB_RELOAD_COOLDOWN_MS = 10000;
  var sbNewChatBound = false;

  function getTextarea() {
    return document.querySelector("textarea");
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

  function populateAndSend(text) {
    populateInput(text);
    // Give React a frame to observe the input change before clicking submit.
    // Retry briefly in case the send button is still disabled (state not flushed).
    var attempts = 0;
    function trySubmit() {
      var btn = document.getElementById("chat-submit");
      if (btn && !btn.disabled) {
        btn.click();
        return;
      }
      if (attempts++ < 10) {
        setTimeout(trySubmit, 30);
      }
    }
    requestAnimationFrame(trySubmit);
  }

  function checkSsoStatus() {
    if (ssoStatusChecked) return;
    ssoStatusChecked = true;

    fetch("../sso-status")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        ssoEnabled = data && data.enabled === true;
        scheduleApply();
      })
      .catch(function () {
        ssoEnabled = false;
      });
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

  function hideReadmeLink() {
    // Hide the Readme link/button
    var buttons = document.querySelectorAll("button, a");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var text = btn.textContent || "";
      var ariaLabel = btn.getAttribute("aria-label") || "";
      var title = btn.getAttribute("title") || "";

      if (text.toLowerCase().trim() === "readme" ||
          ariaLabel.toLowerCase().indexOf("readme") !== -1 ||
          title.toLowerCase().indexOf("readme") !== -1) {
        btn.style.display = "none";
      }
    }
  }

  function ensureLogoutButton() {
    // Single, clean logout button implementation
    // Styled via #sso-logout-button in custom.css

    var existingButton = document.querySelector("#sso-logout-button");

    // If SSO is disabled, remove the button if it exists
    if (ssoEnabled === false) {
      if (existingButton) existingButton.remove();
      return;
    }

    // If SSO status not yet loaded, don't show button yet
    if (ssoEnabled === null) return;

    // SSO is enabled - show the logout button
    if (existingButton) return;

    var button = document.createElement("a");
    button.id = "sso-logout-button";
    button.href = "/chat/auth/logout";
    button.textContent = "Log out";
    button.setAttribute("aria-label", "Sign out of your account");
    document.body.appendChild(button);
  }

  function sbRateLimitOk() {
    try {
      var last = parseInt(sessionStorage.getItem(SB_RELOAD_KEY) || "0", 10);
      return Date.now() - last > SB_RELOAD_COOLDOWN_MS;
    } catch (e) {
      return true;
    }
  }

  function sbMarkReload() {
    try {
      sessionStorage.setItem(SB_RELOAD_KEY, String(Date.now()));
    } catch (e) {}
  }

  function bindNewChatReload() {
    if (sbNewChatBound) return;
    sbNewChatBound = true;

    document.addEventListener(
      "click",
      function (e) {
        var target = e.target;
        if (!target || typeof target.closest !== "function") return;
        var btn = target.closest("#new-chat-button");
        if (!btn) return;
        if (!sbRateLimitOk()) return;
        e.preventDefault();
        e.stopPropagation();
        sbMarkReload();
        location.reload();
      },
      true
    );
  }

  function applyUiUpdates() {
    checkSsoStatus();
    ensureLogoutButton();
    hideReadmeLink();
    hideFeedbackButtons();
    loadStarterData();
    enhanceNativeStarters();
    enhanceSuggestionChips();
    bindNewChatReload();
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
