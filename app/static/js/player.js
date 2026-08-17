// Player page behaviour: quality switching, and polling while a video is
// still being transcoded.
(function () {
  var QUALITY_KEY = 'atomiser:preferred-quality';

  // -------------------------------------------------------------------------
  // Quality selector
  // -------------------------------------------------------------------------
  // The page ships a single <video src>, because a browser given several
  // <source> elements of the same type always takes the first one — which
  // meant the 480p and 360p renditions were transcoded, stored, and never
  // served to anyone. Switching src by hand is what actually makes them usable.
  function initQuality() {
    var video = document.getElementById('video-player');
    var select = document.getElementById('quality-select');
    var note = document.getElementById('quality-note');
    if (!video || !select) return;

    function labelFor(option) {
      return option ? option.getAttribute('data-label') : null;
    }

    function switchTo(option, announce) {
      if (!option || option.value === video.currentSrc) return;

      // Preserve the viewer's place in the video across the source swap.
      var resumeAt = video.currentTime;
      var wasPlaying = !video.paused && !video.ended;

      video.src = option.value;
      video.load();

      video.addEventListener('loadedmetadata', function onLoaded() {
        video.removeEventListener('loadedmetadata', onLoaded);
        if (resumeAt > 0 && isFinite(resumeAt)) {
          try { video.currentTime = resumeAt; } catch (err) { /* seek unsupported */ }
        }
        if (wasPlaying) {
          var playback = video.play();
          if (playback && playback.catch) playback.catch(function () { /* autoplay blocked */ });
        }
        if (announce && note) {
          note.textContent = 'Switched to ' + labelFor(option);
          window.setTimeout(function () { note.textContent = ''; }, 2500);
        }
      });
    }

    // Restore the viewer's last choice if this video has that quality.
    var stored = null;
    try { stored = window.localStorage.getItem(QUALITY_KEY); } catch (err) { /* storage blocked */ }
    if (stored) {
      for (var i = 0; i < select.options.length; i++) {
        if (labelFor(select.options[i]) === stored) {
          select.selectedIndex = i;
          switchTo(select.options[i], false);
          break;
        }
      }
    }

    select.addEventListener('change', function () {
      var option = select.options[select.selectedIndex];
      try { window.localStorage.setItem(QUALITY_KEY, labelFor(option)); } catch (err) { /* storage blocked */ }
      switchTo(option, true);
    });
  }

  // -------------------------------------------------------------------------
  // Processing poll
  // -------------------------------------------------------------------------
  // While the transcode runs the page shows a progress panel instead of an
  // empty player. Poll the status endpoint and reload once it is ready.
  function initProcessing() {
    var panel = document.getElementById('processing-panel');
    if (!panel) return;

    var uuid = panel.getAttribute('data-video-uuid');
    var bar = document.getElementById('processing-bar');
    var statusText = document.getElementById('processing-status');
    var percentText = document.getElementById('processing-percent');
    var message = document.getElementById('processing-message');

    // Back off gradually: a long video can take many minutes, and there is no
    // point hammering the endpoint for the whole of it.
    var delay = 3000;
    var MAX_DELAY = 20000;
    var failures = 0;

    function poll() {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/videos/' + uuid + '/status', true);
      xhr.setRequestHeader('Accept', 'application/json');

      xhr.onload = function () {
        if (xhr.status !== 200) {
          // 403/404 means the video went away or we lost access; stop quietly.
          if (xhr.status === 403 || xhr.status === 404) return;
          scheduleRetry();
          return;
        }

        var data;
        try { data = JSON.parse(xhr.responseText); } catch (err) { scheduleRetry(); return; }
        failures = 0;

        if (data.ready) {
          // Renditions exist now, so a reload renders the real player.
          window.location.reload();
          return;
        }

        if (data.status === 'failed') {
          window.location.reload();
          return;
        }

        if (data.renditions_total > 0) {
          if (bar) {
            bar.classList.remove('indeterminate');
            bar.style.width = data.percent + '%';
          }
          if (statusText) {
            statusText.textContent = data.renditions_ready + ' of ' + data.renditions_total +
              ' quality levels ready';
          }
          if (percentText) percentText.textContent = data.percent + '%';
        } else if (statusText) {
          statusText.textContent = data.status === 'processing' ? 'Preparing…' : 'Queued';
        }

        delay = Math.min(Math.round(delay * 1.25), MAX_DELAY);
        window.setTimeout(poll, delay);
      };

      xhr.onerror = scheduleRetry;
      xhr.send();
    }

    function scheduleRetry() {
      failures++;
      if (failures > 5) {
        if (message) {
          message.textContent = 'Lost contact with the server. Refresh the page to check again.';
        }
        return;
      }
      window.setTimeout(poll, Math.min(delay * 2, MAX_DELAY));
    }

    window.setTimeout(poll, delay);
  }

  initQuality();
  initProcessing();
})();
