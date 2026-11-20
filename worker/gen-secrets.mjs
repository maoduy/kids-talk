// Prints fresh secrets as KEY=value lines (for .dev.vars or `wrangler secret bulk`).
import { generateKeyPairSync, randomBytes, randomInt } from 'node:crypto';
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
const jwk = privateKey.export({ format: 'jwk' });
const raw = Buffer.concat([Buffer.from([4]), Buffer.from(jwk.x, 'base64url'), Buffer.from(jwk.y, 'base64url')]);
const out = {
  AGENT_TOKEN: randomBytes(32).toString('base64url'),
  SETUP_CODE: String(randomInt(10000000, 99999999)),
  SESSION_KEY: randomBytes(32).toString('base64url'),
  VAPID_PUBLIC: raw.toString('base64url'),
  VAPID_PRIVATE_JWK: JSON.stringify({ kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y, d: jwk.d }),
  VAPID_SUBJECT: 'https://kids-talk.invalid',
};
if (process.argv[2] === '--json') console.log(JSON.stringify(out));
else for (const [k, v] of Object.entries(out)) console.log(`${k}=${JSON.stringify(v).startsWith('"{') ? `'${v}'` : v}`);
