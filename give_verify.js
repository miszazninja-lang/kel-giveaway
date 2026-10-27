/* Честный розыгрыш — проверка итога в браузере (или в node 19+). Та же формула, что в give_core.py:
   список «номер код\n» → отпечаток SHA-256 → затравка SHA256("kel-give-v1|id|отпечаток|randomness drand")
   → шаг i: SHA256("затравка|i") mod (сколько билетов осталось). Число drand страница берёт у drand сама. */
(function (root) {
  "use strict";
  var SCHEME = "kel-give-v1";
  var CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971";
  var GENESIS = 1692803367, PERIOD = 3;
  var RELAYS = ["https://api.drand.sh", "https://api2.drand.sh", "https://api3.drand.sh",
                "https://drand.cloudflare.com"];
  var enc = new TextEncoder();

  function hex(buf) {
    return Array.prototype.map.call(new Uint8Array(buf), function (b) {
      return (b < 16 ? "0" : "") + b.toString(16);
    }).join("");
  }
  function sha256(text) {
    return crypto.subtle.digest("SHA-256", enc.encode(text)).then(hex);
  }
  function sha256hex(h) {
    var a = new Uint8Array(h.length / 2);
    for (var i = 0; i < a.length; i++) a[i] = parseInt(h.substr(i * 2, 2), 16);
    return crypto.subtle.digest("SHA-256", a).then(hex);
  }
  function roundTime(r) { return GENESIS + (r - 1) * PERIOD; }
  function parseList(text) {
    return String(text || "").split("\n").map(function (l) { return l.trim(); }).filter(Boolean)
      .map(function (l) { var p = l.split(/\s+/); return [parseInt(p[0], 10), p[1]]; });
  }
  async function beaconOk(b, r) {
    if (!b || Number(b.round) !== Number(r) || !/^[0-9a-f]+$/i.test(b.signature || "")) return false;
    return (await sha256hex(String(b.signature).toLowerCase())) === String(b.randomness).toLowerCase();
  }
  async function fetchBeacon(r, fetchFn) {
    fetchFn = fetchFn || fetch;
    for (var i = 0; i < RELAYS.length; i++) {
      try {
        var resp = await fetchFn(RELAYS[i] + "/" + CHAIN + "/public/" + r);
        if (!resp.ok) continue;
        var b = await resp.json();
        if (await beaconOk(b, r)) {
          return { round: Number(b.round), randomness: String(b.randomness).toLowerCase(),
                   signature: String(b.signature).toLowerCase(), relay: RELAYS[i] };
        }
      } catch (e) { /* следующее зеркало */ }
    }
    return null;
  }
  async function drawOrder(seed, numbers, limit) {
    var left = numbers.map(Number).sort(function (a, b) { return a - b; });
    var out = [];
    for (var i = 0; left.length && (limit == null || out.length < limit); i++) {
      var x = BigInt("0x" + (await sha256(seed + "|" + i)));
      out.push(left.splice(Number(x % BigInt(left.length)), 1)[0]);
    }
    return out;
  }
  async function verify(gid, text, beacon, limit) {
    var digest = await sha256(text);
    var seed = await sha256(SCHEME + "|" + gid + "|" + digest + "|" + beacon.randomness);
    var order = await drawOrder(seed, parseList(text).map(function (e) { return e[0]; }), limit);
    return { digest: digest, seed: seed, order: order };
  }
  root.KelGive = { SCHEME: SCHEME, CHAIN: CHAIN, roundTime: roundTime, sha256: sha256, parseList: parseList,
                   beaconOk: beaconOk, fetchBeacon: fetchBeacon, drawOrder: drawOrder, verify: verify };
})(typeof globalThis !== "undefined" ? globalThis : this);
