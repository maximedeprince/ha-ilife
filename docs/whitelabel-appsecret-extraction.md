# Recovering a whitelabel's app secret

ILIFE ships the same Alibaba Living Link platform under several brand names. Adding a
brand to this integration is a small profile in [`brands.py`](../custom_components/ilife/brands.py):

```python
"zaco": Brand(
    key="zaco", name="ZACO",
    appkey="28416395",
    appsecret=b"????????????????????????????????",   # 32 hex — the only hard part
    oa_app_id="com.zaco.home.robot",
    app_version="1.7.7",
    default_region="eu",
),
```

Three of the four fields are free. `appkey`, `app_version` and the region are sent in the
clear on every API call: capture one request from the brand's app with any HTTPS inspection
proxy (HTTP Toolkit, mitmproxy, PCAPdroid) and read `x-ca-key`, the `appVersion` in the
`request` body, and the hostname (`living-account.eu-central-1.aliyuncs.com` → `eu`).
`oa_app_id` is the Android package id.

`appsecret` is the one that has to be worked for, and this page is how.

## Why it is not in the APK

Every call is signed `HmacSHA1(appSecret, stringToSign)`. The app hands the Java layer a
dummy secret (`setAppSecret("123")`) and delegates the real HMAC to Alibaba's
**SecurityGuard** native library (`libsgmainso-*.so`), which keeps the key in an encrypted
store and decrypts it only while signing. There is nothing to grep for. The plaintext exists
transiently in the process's memory, so it is lifted from the running app.

SecurityGuard also ships its own bundled crypto — `libsgmain` imports no `HMAC_*` or `EVP_*`
from the system `libcrypto`, so hooking BoringSSL never fires. That route is a dead end; do
not spend time on it.

The method below differs from the original AVA writeup in one way that matters: the oracle
is obtained **passively**, by hooking SecurityGuard's own public interface and letting the
app sign on its own. No obfuscated class name to guess, and nothing has to be called by hand.

## What you need

- A **rooted ARM64 Android emulator** with the brand's app installed, logged in, and left in
  the **foreground** — the secret is only resident after the app has signed at least once.
- **frida-server** on the emulator, **frida-tools** on the host.
- Python 3 on the host. No jadx needed for this path.

Throughout, `com.zaco.home.robot` is the ZACO package; substitute your own.

## Step 1 · Capture a signing oracle

A recovered secret is correct if and only if its HMAC reproduces a signature the app itself
produced. So first collect real `(stringToSign, signature)` pairs.

`ISecureSignatureComponent` is SecurityGuard's public API and is **not** obfuscated, so the
implementation can be found by looking for the class that declares `signRequest`.

Save as `oracle.js`:

```js
'use strict';
const PAIRS = [];
rpc.exports.pairs = () => PAIRS;

function dumpCtx(ctx) {
  // SecurityGuardParamContext: appKey + paramMap, whose "INPUT" entry is the
  // stringToSign. Field names are stable across SecurityGuard versions.
  const out = { appKey: null, params: {} };
  try { out.appKey = String(ctx.appKey.value); } catch (e) {}
  try {
    const map = ctx.paramMap.value;
    const Entry = Java.use('java.util.Map$Entry');
    const it = map.entrySet().iterator();
    while (it.hasNext()) {
      const e = Java.cast(it.next(), Entry);
      out.params[String(e.getKey())] = String(e.getValue());
    }
  } catch (e) { out.error = String(e); }
  return out;
}

Java.perform(function () {
  const hooked = [];
  Java.enumerateLoadedClassesSync()
    .filter(n => n.indexOf('com.alibaba.wireless.security') === 0)
    .forEach(function (name) {
      let C;
      try { C = Java.use(name); } catch (e) { return; }
      if (!C.signRequest) return;
      C.signRequest.overloads.forEach(function (ov) {
        ov.implementation = function () {
          const ret = ov.apply(this, arguments);
          const ctx = dumpCtx(arguments[0]);
          const rec = {
            authCode: arguments.length > 1 ? String(arguments[1]) : null,
            appKey: ctx.appKey, params: ctx.params, sig: String(ret),
          };
          PAIRS.push(rec);
          send({ type: 'sig', rec: rec });
          return ret;
        };
      });
      hooked.push(name);
    });
  send({ type: 'ready', hooked: hooked });
});
```

Run it, then **use the app**: pull to refresh, open the vacuum, toggle something.

```sh
frida -U -n com.zaco.home.robot -l oracle.js -o oracle.log
```

You want lines where `params` contains an `INPUT` entry — that string is the `stringToSign`,
and `sig` is its authentic signature. Note the `authCode` too (`"ava"` for AVA); it selects
which key inside the store is used, and it is worth recording in the issue.

If nothing fires, the app has not signed yet: log out and back in, or open a device page.

## Step 2 · Sweep memory for candidates

The secret is resident but **not** next to the appKey — anchoring the scan on the appKey
string finds only GUID-like noise. Sweep every anonymous `rw-` region instead and let the
HMAC test find the needle.

Save as `sweep.js`:

```js
'use strict';

function chunkToLatin1(buf) {
  const u8 = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < u8.length; i += 8192) {
    s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + 8192, u8.length)));
  }
  return s;
}

rpc.exports.sweep = function () {
  const out = {};
  const CHUNK = 256 * 1024;
  Process.enumerateRanges('rw-').forEach(function (r) {
    if (r.file) return;                       // skip mapped files
    if (r.size > 64 * 1024 * 1024) return;    // skip the ~1 GB ART heap
    for (let off = 0; off < r.size; off += CHUNK) {
      const size = Math.min(CHUNK, r.size - off);
      let buf;
      // Frida 17: read through the pointer. The top-level Memory.readByteArray
      // is gone and returns undefined, which fails silently.
      try { buf = r.base.add(off).readByteArray(size); } catch (e) { continue; }
      if (!buf) continue;
      const s = chunkToLatin1(buf);
      const re = /[0-9a-f]{32}/g;
      let m;
      while ((m = re.exec(s)) !== null) out[m[0]] = 1;
    }
  });
  return Object.keys(out);
};
```

```sh
frida -U -n com.zaco.home.robot -l sweep.js -q \
  -e 'rpc.exports.sweep().join("\n")' > candidates.txt
```

Expect a handful to a few dozen candidates. If none of them validate in step 3, widen the
regex to `[0-9a-fA-F]{32}` and then to `[0-9a-zA-Z]{32}`.

## Step 3 · Validate offline

The appSecret is used as the **ASCII bytes of the hex string**, not as decoded binary.

```python
#!/usr/bin/env python3
"""validate.py — find the candidate that reproduces the app's own signatures."""
import base64, hashlib, hmac, json, sys

pairs = json.load(open(sys.argv[1]))        # [{"sts": "...", "sig": "..."}, ...]
cands = [l.strip() for l in open(sys.argv[2]) if l.strip()]

def sign(secret, sts):
    mac = hmac.new(secret.encode(), sts.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()

for c in cands:
    if all(sign(c, p["sts"]) == p["sig"] for p in pairs):
        print("appSecret =", c)
        break
else:
    print(f"no match among {len(cands)} candidates")
```

Build `pairs.json` from the `INPUT` / `sig` values captured in step 1 — **use at least two
different pairs**. One match on a single string can be luck; two cannot.

## Step 4 · Confirm against the real cloud

Sign a real request locally with the recovered secret, with no app and no Frida in the path,
and send it. A `200` with a real payload back means the value is right and the brand profile
can be filled in.

## Frida 17 gotchas

- **Top-level `Memory.readByteArray` is gone.** It returns `undefined` and every region read
  fails silently. Use `ptr.readByteArray(size)`.
- **Do not scan byte-by-byte from JavaScript.** Over ~1 GB of mappings it times out; read in
  chunks and regex each chunk.
- **Enumerating exports across all ~320 modules can crash the app** — SecurityGuard has
  anti-tamper. Scope any native enumeration to the one module you care about.

## Contributing a brand

Open an issue with the four profile fields, the `authCode`, and the confirmation from step 4.
The profile is a few lines and can ship the same day.

The recovered value is a shared *application* secret, not a user credential: account access
still requires the owner's own login. This is interoperability work on a device you own.

---

Original method: [@rtoscani](https://github.com/maximedeprince/ha-ilife/issues/5) for
`com.robot.ava` (appKey `33417005`), which is how the AVA profile in this integration exists.
