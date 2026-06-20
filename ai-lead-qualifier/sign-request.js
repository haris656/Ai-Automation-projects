#!/usr/bin/env node
/**
 * sign-request.js
 *
 * Helper to call the lead-intake webhook with a valid HMAC-SHA256
 * signature. Use this to test the workflow end to end.
 *
 * Usage:
 *   N8N_LEAD_HMAC_SECRET=your_secret \
 *   WEBHOOK_URL=https://your-n8n-host/webhook/lead-intake \
 *   node sign-request.js
 *
 * The signature is the hex HMAC-SHA256 of the exact raw JSON body,
 * sent in the "X-Signature" header. The body must be sent byte for
 * byte identical to what was signed.
 */

const crypto = require('crypto');

const SECRET = process.env.N8N_LEAD_HMAC_SECRET || '';
const URL = process.env.WEBHOOK_URL || 'http://localhost:5678/webhook/lead-intake';

if (!SECRET) {
  console.error('Set N8N_LEAD_HMAC_SECRET to match your n8n environment.');
  process.exit(1);
}

const payload = {
  name: 'Jane Operator',
  email: 'jane@acmelogistics.com',
  company: 'Acme Logistics',
  message: 'We run 12 trucks and want to automate broker calls and invoicing.'
};

// Sign the EXACT string that will be sent as the body.
const body = JSON.stringify(payload);
const signature = crypto.createHmac('sha256', SECRET).update(body).digest('hex');

(async () => {
  const res = await fetch(URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Signature': signature
    },
    body
  });
  const text = await res.text();
  console.log('Status:', res.status);
  console.log('Response:', text);
})();
