.pragma library

// Pure display helpers. No state, no host access: everything here is cheap
// enough to call from a binding on every poll.

var GLYPHS = {
  btc: "󰆬",
  ltc: "󰞾",
  doge: "󰞾",
  eth: "󰞻",
  sol: "◎",
  xmr: "󰗹",
  lightning: "⚡",
  btcwallet: "󰆬"
}

function chainGlyph(chain) {
  return GLYPHS[String(chain || "")] || "\uf0c6"
}

function chainName(chain) {
  return String(chain || "").toUpperCase()
}

function shortAddress(address, head, tail) {
  var value = String(address || "")
  head = head === undefined ? 8 : head
  tail = tail === undefined ? 6 : tail
  if (value.length <= head + tail + 1) return value
  return value.slice(0, head) + "…" + value.slice(-tail)
}

function displayLabel(row) {
  var label = plain(row && row.label)
  if (label !== "") return label
  return shortAddress(row && row.address, row && row.chain === "eth" ? 8 : 6,
                      row && row.chain === "eth" ? 4 : 6)
}

// Drop control characters from provider/user text before it reaches a Text or
// a notification body. Markup is handled by Text.PlainText at every sink.
function plain(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")
    .replace(/[\u202a-\u202e\u2066-\u2069\u200e\u200f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
}

// One relative-time bucket, translated by the caller.
function age(iso, nowMs) {
  if (!iso) return null
  var then = Date.parse(iso)
  if (!isFinite(then)) return null
  var seconds = Math.max(0, Math.floor(((nowMs || Date.now()) - then) / 1000))
  if (seconds < 60) return { unit: "now", value: 0 }
  if (seconds < 3600) return { unit: "minutes", value: Math.floor(seconds / 60) }
  if (seconds < 86400) return { unit: "hours", value: Math.floor(seconds / 3600) }
  return { unit: "days", value: Math.floor(seconds / 86400) }
}

function clamp(value, min, max) {
  var number = Number(value)
  if (!isFinite(number)) return min
  return Math.max(min, Math.min(max, number))
}
