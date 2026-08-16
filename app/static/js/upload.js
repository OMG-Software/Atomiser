// Chunked upload with progress.
//
// Browsers cannot report byte-level progress for a plain form POST, and the
// site sits behind Cloudflare, which caps a single request body at 100 MB on
// the Free/Pro plan. So instead of one big POST, the file is split into
// <100 MB chunks and each is POSTed to /upload/chunk; a final /upload/complete
// tells the server to validate the assembled file and start transcoding.
//
// The server-side endpoints are in app/videos.py. With JS disabled the form
// still submits normally to the single-shot /upload route (small files only).
(function () {
  var form = document.getElementById('upload-form');
  if (!form) return;

  var progress = document.getElementById('upload-progress');
  var bar = document.getElementById('upload-progress-bar');
  var label = document.getElementById('upload-progress-label');
  var statusText = document.getElementById('upload-progress-status');
  var submitBtn = form.querySelector('button[type=submit]');
  var submitLabel = submitBtn ? submitBtn.textContent : 'Upload video';
  var fileInput = form.querySelector('#video');
  var csrfInput = form.querySelector('input[name=csrf]');

  // 50 MB — comfortably under Cloudflare's 100 MB edge body cap, with room for
  // multipart overhead (boundaries + the small form fields in each request).
  var CHUNK_SIZE = 50 * 1024 * 1024;
  var MAX_RETRIES = 3;

  function humanBytes(n) {
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }

  function setBar(pct, status, lbl) {
    if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
    if (label && lbl !== undefined) label.textContent = lbl;
    if (statusText && status !== undefined) statusText.textContent = status;
  }

  function fail(msg) {
    if (progress) progress.classList.add('error');
    if (bar) bar.classList.remove('done');
    if (statusText) { statusText.textContent = msg; statusText.classList.add('error'); }
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = submitLabel; }
  }

  function decodeEntities(s) {
    var d = document.createElement('div');
    d.innerHTML = s;
    return (d.textContent || d.innerText || s).trim();
  }

  // The server's global error handler renders <p class="meta">{{ detail }}</p>;
  // pull that out so the user sees the actual reason (e.g. "Unsupported video
  // format", "Upload too large").
  function extractError(xhr) {
    var html = xhr.responseText || '';
    var m = html.match(/<p class="meta">(.*?)<\/p>/);
    if (m) return decodeEntities(m[1]);
    if (xhr.status === 413) return 'Upload too large.';
    if (xhr.status === 0) return 'Network error — could not reach the server.';
    return 'Upload failed (' + xhr.status + ').';
  }

  function makeUploadId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    return 'up_' + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();

    if (!fileInput || !fileInput.files || !fileInput.files.length) return;

    // Client-side size guard: reject oversize files before we start uploading.
    // Both nginx and the app reject an oversize upload from Content-Length
    // while the body is still streaming, which surfaces as ERR_CONNECTION_RESET
    // rather than a readable error. Checking file.size locally avoids that.
    var maxBytes = 0;
    if (form.dataset.maxUploadMb) {
      maxBytes = parseInt(form.dataset.maxUploadMb, 10) * 1024 * 1024;
    }
    var file = fileInput.files[0];
    if (maxBytes > 0 && file.size > maxBytes) {
      if (progress) progress.hidden = false;
      setBar(0, undefined, '0%');
      fail('This file is ' + humanBytes(file.size) + ', which exceeds the ' +
           humanBytes(maxBytes) + ' upload limit.');
      return;
    }

    var csrf = csrfInput ? csrfInput.value : '';
    var meta = {
      title: (form.querySelector('#title') || {}).value || '',
      description: (form.querySelector('#description') || {}).value || '',
      visibility: (form.querySelector('#visibility') || {}).value || 'site',
      filename: file.name,
      mime: file.type,
    };
    var total = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
    var uploadId = makeUploadId();

    if (progress) { progress.hidden = false; progress.classList.remove('error'); }
    if (statusText) statusText.classList.remove('error');
    if (bar) bar.classList.remove('done');
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Uploading…'; }
    setBar(0, 'Uploading…', '0%');

    var uploadedBytes = 0;

    function sendChunk(i) {
      if (i >= total) return complete();

      var start = i * CHUNK_SIZE;
      var end = Math.min(start + CHUNK_SIZE, file.size);
      var blob = file.slice(start, end);
      var fd = new FormData();
      fd.append('csrf', csrf);
      fd.append('upload_id', uploadId);
      fd.append('index', i);
      fd.append('total', total);
      fd.append('chunk_size', CHUNK_SIZE);
      fd.append('total_size', file.size);
      fd.append('filename', meta.filename);
      fd.append('mime', meta.mime);
      fd.append('title', meta.title);
      fd.append('description', meta.description);
      fd.append('visibility', meta.visibility);
      fd.append('chunk', blob, meta.filename);

      doSend(i, fd, start, end, 0);
    }

    function doSend(i, fd, start, end, attempt) {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload/chunk', true);

      xhr.upload.onprogress = function (ev) {
        if (ev.lengthComputable && file.size > 0) {
          var overall = uploadedBytes + ev.loaded;
          var pct = Math.min(100, Math.round((overall / file.size) * 100));
          setBar(pct, 'Uploading… chunk ' + (i + 1) + '/' + total + ' · ' + pct + '%', pct + '%');
        }
      };

      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 400) {
          uploadedBytes += (end - start);
          sendChunk(i + 1);
        } else if (attempt < MAX_RETRIES && (xhr.status === 0 || xhr.status >= 500)) {
          // Transient — retry the same chunk after a short backoff.
          setTimeout(function () { doSend(i, fd, start, end, attempt + 1); }, 1000 * (attempt + 1));
        } else {
          fail(extractError(xhr));
        }
      };

      xhr.onerror = xhr.ontimeout = function () {
        if (attempt < MAX_RETRIES) {
          setTimeout(function () { doSend(i, fd, start, end, attempt + 1); }, 1000 * (attempt + 1));
        } else {
          fail('Network error during chunk ' + (i + 1) + ' of ' + total + '.');
        }
      };

      xhr.send(fd);
    }

    function complete() {
      if (bar) bar.classList.add('done');
      setBar(100, 'Processing…', '100%');

      var fd = new FormData();
      fd.append('csrf', csrf);
      fd.append('upload_id', uploadId);

      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload/complete', true);
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 400) {
          var uuid = null;
          try { uuid = (JSON.parse(xhr.responseText) || {}).uuid; } catch (e) {}
          if (uuid) {
            if (statusText) statusText.textContent = 'Upload complete. Redirecting…';
            window.location.href = '/videos/' + uuid;
          } else {
            window.location.href = '/';
          }
        } else {
          if (bar) bar.classList.remove('done');
          fail(extractError(xhr));
        }
      };
      xhr.onerror = function () {
        if (bar) bar.classList.remove('done');
        fail('Network error finalising the upload.');
      };
      xhr.send(fd);
    }

    sendChunk(0);
  });

  // Reset the UI if the user picks a different file after a failed upload.
  if (fileInput) {
    fileInput.addEventListener('change', function () {
      if (progress && progress.classList.contains('error')) {
        if (progress) progress.hidden = true;
        if (progress) progress.classList.remove('error');
        if (bar) bar.style.width = '0%';
        if (label) label.textContent = '0%';
        if (statusText) { statusText.textContent = ''; statusText.classList.remove('error'); }
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = submitLabel; }
      }
    });
  }
})();