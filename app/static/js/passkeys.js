function base64urlToBuffer(base64url) {
  // Accept both base64url and standard base64 (with or without padding)
  const base64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');
  const binary = atob(padded);
  const buffer = new ArrayBuffer(binary.length);
  const view = new Uint8Array(buffer);
  for (let i = 0; i < binary.length; i++) {
    view[i] = binary.charCodeAt(i);
  }
  return buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function decodeCredentialOptions(options) {
  if (options.challenge) {
    options.challenge = base64urlToBuffer(options.challenge);
  }
  if (options.user && options.user.id) {
    options.user.id = base64urlToBuffer(options.user.id);
  }
  if (options.excludeCredentials) {
    options.excludeCredentials = options.excludeCredentials.map(c => ({
      ...c,
      id: base64urlToBuffer(c.id),
    }));
  }
  return options;
}

function decodeAssertionOptions(options) {
  if (options.challenge) {
    options.challenge = base64urlToBuffer(options.challenge);
  }
  if (options.allowCredentials) {
    options.allowCredentials = options.allowCredentials.map(c => ({
      ...c,
      id: base64urlToBuffer(c.id),
    }));
  }
  return options;
}

// Register passkey
const registerForm = document.getElementById('passkey-register-form');
if (registerForm) {
  registerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const csrf = registerForm.querySelector('input[name="csrf"]').value;
    const msg = document.getElementById('passkey-msg');
    try {
      const optsResp = await fetch('/auth/passkey/register/options', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ csrf }),
      });
      if (!optsResp.ok) throw new Error(await optsResp.text());
      const options = decodeCredentialOptions(await optsResp.json());

      const credential = await navigator.credentials.create({ publicKey: options });

      const response = credential.response;
      const body = {
        csrf,
        id: credential.id,
        rawId: bufferToBase64url(credential.rawId),
        type: credential.type,
        response: {
          clientDataJSON: bufferToBase64url(response.clientDataJSON),
          attestationObject: bufferToBase64url(response.attestationObject),
          transports: response.getTransports ? response.getTransports() : [],
        },
      };

      const verifyResp = await fetch('/auth/passkey/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!verifyResp.ok) throw new Error(await verifyResp.text());
      if (msg) msg.textContent = 'Passkey registered. Reload the page to see it.';
      else window.location.reload();
    } catch (err) {
      console.error(err);
      if (msg) msg.textContent = 'Failed: ' + err.message;
    }
  });
}

// Login with passkey
const loginForm = document.getElementById('passkey-login-form');
if (loginForm) {
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('pk-email').value;
    const csrf = loginForm.querySelector('input[name="csrf"]').value;
    try {
      const optsResp = await fetch('/auth/passkey/login/options', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, csrf }),
      });
      if (!optsResp.ok) throw new Error(await optsResp.text());
      const { options, temp_token } = await optsResp.json();

      const assertion = await navigator.credentials.get({
        publicKey: decodeAssertionOptions(options),
      });

      const response = assertion.response;
      const body = {
        temp_token,
        id: assertion.id,
        rawId: bufferToBase64url(assertion.rawId),
        type: assertion.type,
        response: {
          clientDataJSON: bufferToBase64url(response.clientDataJSON),
          authenticatorData: bufferToBase64url(response.authenticatorData),
          signature: bufferToBase64url(response.signature),
          userHandle: response.userHandle ? bufferToBase64url(response.userHandle) : null,
        },
      };

      const verifyResp = await fetch('/auth/passkey/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!verifyResp.ok) throw new Error(await verifyResp.text());
      window.location.href = '/';
    } catch (err) {
      console.error(err);
      alert('Passkey login failed: ' + err.message);
    }
  });
}
