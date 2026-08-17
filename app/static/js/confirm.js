// Confirmation prompts for destructive forms.
//
// These used to be inline onsubmit="return confirm(...)" attributes, which the
// site's own Content-Security-Policy (script-src 'self', no 'unsafe-inline')
// blocks — so in production the confirm never ran and the form submitted
// straight through. A delegated listener in an external file works under the
// CSP and keeps the message out of an HTML attribute, where apostrophes in a
// video title would otherwise break the quoting.
(function () {
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.matches || !form.matches('form[data-confirm]')) return;
    if (!window.confirm(form.getAttribute('data-confirm'))) {
      e.preventDefault();
    }
  });
})();
