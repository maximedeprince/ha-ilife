# Recovering a whitelabel's app secret

ILIFE ships the same Alibaba Living Link platform under several brand names. Adding a
brand to this integration is a small profile in [`brands.py`](../custom_components/ilife/brands.py):

```python
"mybrand": Brand(
    key="mybrand", name="My Brand",
    appkey="12345678",
    appsecret=b"????????????????????????????????",   # 32 hex — the only hard part
    oa_app_id="com.mybrand.home",
    app_version="1.0.0",
    default_region="eu",
),
```

Three of the four fields are free. `appkey`, `app_version` and the region are sent in the
clear on every API call: capture one request from the brand's app with any HTTPS inspection
proxy (HTTP Toolkit, mitmproxy, PCAPdroid) and read `x-ca-key`, the `appVersion` in the
`request` body, and the hostname (`living-account.eu-central-1.aliyuncs.com` → `eu`).
`oa_app_id` is the Android package id.

`appsecret` is the one that has to be worked for, and this page is how.

## Where the secret is

Every call is signed `HmacSHA1(appSecret, stringToSign)`. Apps do one of two things with
the secret:

- **They set it from Java.** ILIFE (`com.ilife.home.global`) and ZACO
  (`com.zaco.home.robot`) hand the real secret to the SDK from their own code, so it is a
  plain string in the APK. Step 1 finds it in a minute.
- **They leave it to SecurityGuard.** AVA (`com.robot.ava`) gives the Java layer a dummy
  (`setAppSecret("123")`) and delegates the HMAC to Alibaba's **SecurityGuard** native
  library (`libsgmainso-*.so`), which keeps the key in an encrypted store and decrypts it
  only while signing. Then it exists in plaintext only transiently, in the running app's
  memory, and step 2 lifts it from there.

SecurityGuard also ships its own bundled crypto — `libsgmain` imports no `HMAC_*` or `EVP_*`
from the system `libcrypto`, so hooking BoringSSL never fires. That route is a dead end; do
not spend time on it.

Either way, step 3 tells the right candidate from the wrong ones by asking Aliyun's API
gateway, which checks the signature before anything else. No account, no app and no signing
oracle are needed for that part.

## What you need

- Step 1: the brand's APK and a shell.
- Step 2 (only if step 1 finds nothing): a **rooted ARM64 Android emulator** with the
  brand's app installed, logged in, and left in the **foreground** — the secret is only
  resident after the app has signed at least once — plus **frida-server** on the emulator and
  **frida-tools** on the host.
- Step 3: Python 3 and an internet connection.

Throughout, `com.zaco.home.robot` is the ZACO package; substitute your own.

## Step 1 · Look in the APK

```sh
unzip -o app.apk 'classes*.dex' -d dex/
grep -aohE '[0-9a-f]{32}' dex/classes*.dex | sort -u > candidates.txt
```

For a split APK (`.xapk`, `.apks`), use the base APK inside it. Then go to step 3. If one of
the candidates is accepted, you are done; if none is, the app keeps its secret in
SecurityGuard, and step 2 is next.

## Step 2 · Sweep memory for candidates

The secret is resident but **not** next to the appKey — anchoring the scan on the appKey
string finds only GUID-like noise. Sweep every anonymous `rw-` region instead and let step 3
find the needle.

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

Expect a few dozen to a hundred candidates (93 for ZACO). On Windows, PowerShell's `>` writes
the file in UTF-16; step 3 reads either encoding. If none of them is accepted, widen the regex
to `[0-9a-fA-F]{32}` and then to `[0-9a-zA-Z]{32}`.

## Step 3 · Ask the gateway

The app's first call, `connect.json`, needs no account and no token, but it is signed like
every other call, and the gateway checks the signature before it does anything else. Signed
with the right secret, it gets a normal reply; with a wrong one, `403 Invalid Signature`. So
the gateway itself says which candidate is the secret.

The appSecret is used as the **ASCII bytes of the hex string**, not as decoded binary.

Save as `gateway_check.py`:

```python
#!/usr/bin/env python3
"""gateway_check.py — find the app secret among candidates by asking Aliyun's gateway.

    python3 gateway_check.py <appKey> <appVersion> candidates.txt

Signs the app's first call, connect.json (no account, no token), with each candidate.
The gateway checks the signature before anything else: the right secret gets a normal
reply, a wrong one gets 403 "Invalid Signature". Standard library only; reads the
candidates file in UTF-8 or UTF-16 (PowerShell's `>` writes UTF-16).
"""
import base64, email.utils, hashlib, hmac, json, re, sys, time, urllib.error, \
    urllib.parse, urllib.request, uuid

HOST, PATH = "sdk.openaccount.aliyun.com", "/api/prd/connect.json"


def candidates(path):
    raw = open(path, "rb").read()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode()
    seen = []
    for found in re.findall(r"[0-9a-fA-F]{32}", text.replace(" ", "").replace("\x00", "")):
        if found not in seen:
            seen.append(found)
    return seen


def connect(app_key, app_version, secret):
    form = {"request": json.dumps({
        "context": {"sdkVersion": "3.4.2", "platformName": "android", "netType": "wifi",
                    "appKey": app_key, "appVersion": app_version, "utDid": "",
                    "yunOSId": "", "appAuthToken": "", "securityToken": ""},
        "config": {"version": 0, "lastModify": 0},
        "device": {"model": "sdk_gphone64_arm64", "brand": "google",
                   "platformVersion": "30"}})}
    accept = "application/json; charset=utf-8"
    ctype = "application/x-www-form-urlencoded; charset=utf-8"
    date = email.utils.formatdate(usegmt=True)
    signed = {"x-ca-key": app_key, "x-ca-nonce": str(uuid.uuid4()),
              "x-ca-signature-method": "HmacSHA1",
              "x-ca-timestamp": str(int(time.time() * 1000))}
    sts = (f"POST\n{accept}\n\n{ctype}\n{date}\n"
           + "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
           + PATH + "?" + "&".join(f"{k}={form[k]}" for k in sorted(form)))
    sig = base64.b64encode(hmac.new(secret.encode(), sts.encode(), hashlib.sha1).digest())
    headers = {**signed, "date": date, "accept": accept, "content-type": ctype,
               "x-ca-signature": sig.decode(), "ca_version": "1",
               "x-ca-signature-headers": ",".join(sorted(signed))}
    body = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(f"https://{HOST}{PATH}", body, headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("X-Ca-Error-Message", "")


def main():
    app_key, app_version, path = sys.argv[1:4]
    found = candidates(path)
    print(f"{len(found)} candidates")
    for index, secret in enumerate(found, 1):
        status, detail = connect(app_key, app_version, secret)
        if status == 200:
            print(f"appSecret = {secret}  (candidate {index}, gateway reply 200)")
            return
        if status != 403 or "Signature" not in str(detail):
            print(f"candidate {index}: unexpected reply {status} {str(detail)[:80]}")
        time.sleep(0.3)
    print("no candidate is accepted: widen the sweep, or check appKey and region")


if __name__ == "__main__":
    main()
```

```sh
python3 gateway_check.py 28416395 1.7.7 candidates.txt
```

Run on the ZACO candidates posted in [#14](https://github.com/maximedeprince/ha-ilife/issues/14),
it accepts candidate 90 of 93: the secret the ZACO profile now uses. A reply of 200 is the
whole proof; the profile can be filled in.

## Alternative: validate offline against the app's own signatures

If you would rather not send requests, the app can be made to show what it signs, and the
candidates checked offline. This is how the AVA secret was found.

### Capture a signing oracle

A recovered secret is correct if and only if its HMAC reproduces a signature the app itself
produced. `ISecureSignatureComponent` is SecurityGuard's public API and is **not**
obfuscated, so the implementation can be found by looking for the class that declares
`signRequest`.

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
and `sig` is its authentic signature. If nothing fires, the app has not signed yet: log out
and back in, or open a device page. (On an app that sets its secret from Java, SecurityGuard
may never sign at all — another reason to start with step 1 and step 3.)

### Validate

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

Build `pairs.json` from the `INPUT` / `sig` values captured above — **use at least two
different pairs**. One match on a single string can be luck; two cannot.

## Frida 17 gotchas

- **Top-level `Memory.readByteArray` is gone.** It returns `undefined` and every region read
  fails silently. Use `ptr.readByteArray(size)`.
- **Do not scan byte-by-byte from JavaScript.** Over ~1 GB of mappings it times out; read in
  chunks and regex each chunk.
- **Enumerating exports across all ~320 modules can crash the app** — SecurityGuard has
  anti-tamper. Scope any native enumeration to the one module you care about.

## Contributing a brand

Open an issue with the four profile fields and the output of `gateway_check.py`. The profile
is a few lines and can ship the same day.

The recovered value is a shared *application* secret, not a user credential: account access
still requires the owner's own login. This is interoperability work on a device you own.

---

Original method: [@rtoscani](https://github.com/maximedeprince/ha-ilife/issues/5) for
`com.robot.ava` (appKey `33417005`), which is how the AVA profile in this integration exists.
The gateway check came out of [#14](https://github.com/maximedeprince/ha-ilife/issues/14),
where @FF0205's sweep had already found the ZACO secret and only the oracle step stood in
the way.
