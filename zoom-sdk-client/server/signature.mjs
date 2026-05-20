// Minimal signature server for the Zoom Meeting SDK.
// The SDK secret MUST stay on the server — never ship it to the browser.
//
// Run:
//   npm i express
//   ZOOM_SDK_KEY=... ZOOM_SDK_SECRET=... node server/signature.mjs
//
// Then point app.js `signatureEndpoint` at http://localhost:8787/api/signature
// (or proxy /api/* through whatever serves index.html).

import crypto from 'node:crypto';
import express from 'express';

const { ZOOM_SDK_KEY, ZOOM_SDK_SECRET } = process.env;
if (!ZOOM_SDK_KEY || !ZOOM_SDK_SECRET) {
  throw new Error('ZOOM_SDK_KEY and ZOOM_SDK_SECRET env vars are required');
}

function b64url(input) {
  return Buffer.from(input).toString('base64url');
}

function generateSignature({ meetingNumber, role = 0, expSeconds = 60 * 60 * 2 }) {
  const iat = Math.floor(Date.now() / 1000) - 30;
  const exp = iat + expSeconds;
  const header = { alg: 'HS256', typ: 'JWT' };
  const payload = {
    sdkKey: ZOOM_SDK_KEY,
    appKey: ZOOM_SDK_KEY,
    mn: String(meetingNumber),
    role,
    iat,
    exp,
    tokenExp: exp,
  };
  const unsigned = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
  const sig = crypto
    .createHmac('sha256', ZOOM_SDK_SECRET)
    .update(unsigned)
    .digest('base64url');
  return `${unsigned}.${sig}`;
}

const app = express();
app.use(express.json({ limit: '8kb' }));

// CORS allowlist. Default: localhost only. Override with
// ALLOWED_ORIGINS=https://app.example.com,https://other.example.com
// Setting ALLOWED_ORIGINS=* opts back into wildcard (NOT recommended —
// the SDK secret signs JWTs, so unrestricted callers can mint Zoom
// signatures against arbitrary meetings).
const _origins = (process.env.ALLOWED_ORIGINS || 'http://localhost:8787,http://127.0.0.1:8787')
  .split(',').map(o => o.trim()).filter(Boolean);
const _allowAny = _origins.includes('*');

app.use((req, res, next) => {
  const origin = req.headers.origin;
  if (_allowAny) {
    res.setHeader('Access-Control-Allow-Origin', '*');
  } else if (origin && _origins.includes(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Vary', 'Origin');
  }
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

app.post('/api/signature', (req, res) => {
  try {
    const { meetingNumber, role = 0 } = req.body || {};
    // Meeting numbers are digit strings (Zoom returns them as 9-11 digits).
    // Reject anything else so we don't sign arbitrary attacker-supplied
    // payloads into the JWT `mn` claim.
    if (typeof meetingNumber !== 'string' && typeof meetingNumber !== 'number') {
      return res.status(400).json({ error: 'meetingNumber required' });
    }
    const mnStr = String(meetingNumber);
    if (!/^\d{8,12}$/.test(mnStr)) {
      return res.status(400).json({ error: 'invalid meetingNumber' });
    }
    const roleNum = Number(role);
    if (!Number.isInteger(roleNum) || (roleNum !== 0 && roleNum !== 1)) {
      return res.status(400).json({ error: 'invalid role' });
    }
    const signature = generateSignature({ meetingNumber: mnStr, role: roleNum });
    res.json({ signature });
  } catch (err) {
    res.status(500).json({ error: 'signature generation failed' });
  }
});

const port = Number(process.env.PORT || 8787);
app.listen(port, () => console.log(`signature server listening on :${port}`));
