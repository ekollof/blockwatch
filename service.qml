import QtQuick
import Quickshell
import Quickshell.Networking
import qs.Commons
import "omakit"
import "Model.js" as ModelPlain

// Headless service: one poller and one notification stream for every bar
// instance. The bar widget only draws this state and sends edit requests, so a
// four-monitor bar still makes one provider call per interval and one
// notification per event.
// All network work, validation and state live in bin/blockwatch; this object
// owns processes, the timer, the unread count and the desktop notification.
Item {
  id: root

  property var settings: null
  property var chains: []
  property var servers: []
  property var addresses: []
  property var events: []
  property var errors: []
  property string checkedAt: ""
  property string warning: ""
  property string lastError: ""
  property bool ready: false
  property bool busy: false
  property int unread: 0
  property bool actionBusy: false
  property string actionError: ""
  property string actionKind: ""
  property string actionTarget: ""

  property bool pendingPoll: false
  property bool pendingStatus: false
  property bool started: false
  property int widgetCount: 0
  property var pendingMutation: null
  property bool cancellingPoll: false

  signal actionFinished(bool ok, string message)

  Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  readonly property string helperPath: decodeURIComponent(
    Qt.resolvedUrl("bin/blockwatch").toString().replace(/^file:\/\//, ""))

  readonly property string configPath: {
    var base = Quickshell.env("XDG_CONFIG_HOME") || ((Quickshell.env("HOME") || "") + "/.config")
    return base + "/blockwatch/config.json"
  }
  readonly property string statePath: {
    var base = Quickshell.env("XDG_STATE_HOME") || ((Quickshell.env("HOME") || "") + "/.local/state")
    return base + "/blockwatch/state.json"
  }

  readonly property var baseCommand: [
    "/usr/bin/python3", "-I", "-S", "-B", root.helperPath,
    "--config", root.configPath,
    "--state", root.statePath
  ]

  readonly property int intervalSec: {
    var value = Number(setting("refreshIntervalSec", 120))
    if (!isFinite(value)) value = 120
    return Math.min(3600, Math.max(30, Math.round(value)))
  }
  readonly property bool notifyEnabled: root.settings !== null
    && setting("notify", true) === true
  readonly property int addressCount: addresses.length
  readonly property bool hasAddresses: addresses.length > 0
  readonly property bool online: Networking.connectivity === NetworkConnectivity.Full

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function loadStatus() {
    if (pollRun.running || statusRun.running || actionRun.running) {
      pendingStatus = true
      return
    }
    statusRun.start()
  }

  function poll() {
    if (root.cancellingPoll || root.pendingMutation) {
      pendingPoll = true
      return
    }
    if (pollRun.running || statusRun.running || actionRun.running) {
      pendingPoll = true
      return
    }
    busy = true
    lastError = ""
    pollRun.start()
  }

  function markRead() {
    unread = 0
  }

  // The service exists only to feed a bar widget. With no widget attached,
  // the timer stays stopped and no notification is produced, so removing the
  // widget from the bar also stops the watcher.
  function attachWidget() {
    widgetCount += 1
    if (!started && hasAddresses && widgetCount > 0) {
      started = true
      Qt.callLater(root.poll)
    }
  }

  function detachWidget() {
    widgetCount = Math.max(0, widgetCount - 1)
  }

  function addAddress(chain, address, label, viewKey, mode, endpoint) {
    queueMutation("add", {
      chain: chain,
      address: address,
      label: label,
      viewKey: viewKey,
      mode: mode,
      endpoint: endpoint,
    })
  }

  function updateAddress(id, chain, address, label, viewKey, mode, endpoint) {
    queueMutation("update", {
      id: id,
      chain: chain,
      address: address,
      label: label,
      viewKey: viewKey,
      mode: mode,
      endpoint: endpoint,
    })
  }

  function removeAddress(id) {
    queueMutation("remove", { id: id })
  }

  // A config mutation must not race a poll that already holds the old watch
  // list: that poll could still be about to contact an address the user just
  // stopped, including its light-wallet server. Cancel it, wait for the
  // process group to be gone, then apply the edit.
  function queueMutation(kind, data) {
    if (actionRun.running || actionBusy) return
    if (pollRun.running) {
      pendingMutation = { kind: kind, data: data }
      cancellingPoll = true
      actionBusy = true
      pendingPoll = false
      pollRun.cancel()
      return
    }
    executeMutation(kind, data)
  }

  function executeMutation(kind, data) {
    if (actionRun.running || actionBusy) return
    var args = []
    var viewKey = ""
    if (kind === "add") {
      args = baseCommand.concat(["add", "--chain", String(data.chain),
                                 "--address", String(data.address)])
      if (String(data.label || "").trim() !== "")
        args = args.concat(["--label", String(data.label).trim()])
      if (String(data.mode || "") !== "")
        args = args.concat(["--mode", String(data.mode)])
      if (String(data.endpoint || "").trim() !== "")
        args = args.concat(["--endpoint", String(data.endpoint).trim()])
      viewKey = data.viewKey
    } else if (kind === "update") {
      args = baseCommand.concat([
        "update", String(data.id),
        "--chain", String(data.chain),
        "--address", String(data.address),
        "--label", String(data.label || "")
      ])
      if (String(data.mode || "") !== "")
        args = args.concat(["--mode", String(data.mode)])
      if (String(data.endpoint || "").trim() !== "")
        args = args.concat(["--endpoint", String(data.endpoint).trim()])
      viewKey = data.viewKey
    } else if (kind === "remove") {
      args = baseCommand.concat(["remove", String(data.id)])
    } else {
      return
    }
    actionKind = kind
    actionTarget = String(data.label || data.address || data.id || "")
    actionError = ""
    actionBusy = true
    actionRun.environment = { "BLOCKWATCH_VIEW_KEY": String(viewKey || "").trim() }
    actionRun.command = args
    actionRun.start()
  }

  function parseReport(text) {
    if (typeof text !== "string" || text.length === 0 || text.length > 1048576) return null
    var document
    try {
      document = JSON.parse(text)
    } catch (error) {
      return null
    }
    if (!document || document.ok !== true || !Array.isArray(document.addresses)) return null
    if (document.addresses.length > 200) return null
    if (!Array.isArray(document.events)) document.events = []
    if (!Array.isArray(document.errors)) document.errors = []
    if (Array.isArray(document.servers) && document.servers.length > 100)
      document.servers = document.servers.slice(0, 100)
    var rows = []
    for (var i = 0; i < document.addresses.length; i++) {
      var row = document.addresses[i]
      if (!row || typeof row !== "object") continue
      if (typeof row.id !== "string" || typeof row.chain !== "string") continue
      if (typeof row.address !== "string") continue
      if (!Array.isArray(row.newEvents)) row.newEvents = []
      rows.push(row)
    }
    document.addresses = rows
    document.events = document.events.slice(0, 100)
    document.errors = document.errors.slice(0, 100)
    return document
  }

  function applyReport(document) {
    if (Array.isArray(document.chains) && document.chains.length > 0)
      root.chains = document.chains
    if (Array.isArray(document.servers))
      root.servers = document.servers
    root.addresses = document.addresses
    root.checkedAt = typeof document.checkedAt === "string" ? document.checkedAt : ""
    root.warning = typeof document.warning === "string" ? document.warning : ""
    root.ready = true
    root.errors = document.errors
    root.events = document.events
  }

  function helperError(result) {
    if (typeof result.stdout !== "string" || result.stdout.length === 0
        || result.stdout.length > 1048576) return null
    try {
      var document = JSON.parse(result.stdout)
    } catch (error) {
      return null
    }
    if (document && typeof document.error === "string" && document.error !== "")
      return document.error
    return null
  }

  function reportFailure(state, reason, stderr) {
    var detail = String(reason || "").trim()
    if (detail === "") detail = String(stderr || "").trim()
    if (detail.length > 200) detail = detail.slice(0, 200)
    if (state === "timeout") root.lastError = "check timed out"
    else if (state === "spawn-failed") root.lastError = "could not start the helper"
    else if (state === "python-missing") root.lastError = "python3 is missing"
    else root.lastError = detail !== "" ? detail : ("check failed (" + state + ")")
  }

  function sendNotification(title, body) {
    Util.execArgv(["omarchy-notification-send", "--app-name", "blockwatch",
                   "-g", "󰈈", "-u", "normal", title, body])
  }

  function notificationText(value) {
    // The notification daemon accepts a small markup subset, so labels are
    // stripped of tag and entity delimiters rather than passed as markup.
    return ModelPlain.plain(value).replace(/[<>&]/g, "")
  }

  function eventBody(event) {
    var label = root.notificationText(event.label)
    if (label === "") label = root.notificationText(
      ModelPlain.shortAddress(event.address, 6, 4))
    var amount = root.notificationText(event.amount)
    var symbol = root.notificationText(event.symbol)
    if (event.kind === "in" && amount !== "")
      return strings.t("notification.received").arg(label).arg(amount).arg(symbol)
    if (event.kind === "out" && amount !== "")
      return strings.t("notification.sent").arg(label).arg(amount).arg(symbol)
    if (event.kind === "self" && amount !== "")
      return strings.t("notification.self").arg(label).arg(amount).arg(symbol)
    if (event.kind === "balance" && amount !== "")
      return strings.t("notification.balance").arg(label).arg(amount).arg(symbol)
    return strings.t("notification.activity").arg(label)
  }

  function notifyEvents(events) {
    if (!notifyEnabled || !Array.isArray(events) || events.length === 0) return
    var limit = 3
    var shown = Math.min(limit, events.length)
    for (var i = 0; i < shown; i++) {
      var event = events[i] || {}
      var chain = root.notificationText(event.chain).toUpperCase()
      sendNotification(strings.t("notification.title").arg(chain), eventBody(event))
    }
    if (events.length > limit) {
      var extra = events.length - limit
      sendNotification(strings.t("popup.title"),
                       strings.tn("notification.more", extra).arg(extra))
    }
  }

  function handleReport(document) {
    var fresh = Array.isArray(document.events) ? document.events : []
    if (fresh.length === 0) return
    if (widgetCount === 0) return
    unread = Math.min(999, unread + fresh.length)
    notifyEvents(fresh)
  }

  Timer {
    id: pollTimer
    interval: root.intervalSec * 1000
    running: root.hasAddresses && root.widgetCount > 0
    repeat: true
    onTriggered: root.poll()
  }

  onOnlineChanged: {
    if (root.online && root.hasAddresses && root.widgetCount > 0 && !root.busy) root.poll()
  }

  Run {
    id: statusRun
    command: root.baseCommand.concat(["status"])
    deadlineMs: 15000
    maxBytes: 1048576
    keepBytes: 1048576
    onFinished: function(result) {
      if (result.state !== "ok") {
        var failure = root.helperError(result)
        if (failure !== null) root.lastError = failure
        else root.reportFailure(result.state, result.reason, result.stderr)
      } else {
        var document = root.parseReport(result.stdout)
        if (document === null) root.lastError = "the helper returned an unreadable report"
        else {
          root.lastError = ""
          root.applyReport(document)
        }
      }
      if (!root.started && root.hasAddresses && root.widgetCount > 0) {
        root.started = true
        Qt.callLater(root.poll)
      }
      if (root.pendingPoll) {
        root.pendingPoll = false
        Qt.callLater(root.poll)
      } else if (root.pendingStatus) {
        root.pendingStatus = false
        Qt.callLater(root.loadStatus)
      }
    }
  }

  Run {
    id: pollRun
    command: root.baseCommand.concat(["poll"])
    deadlineMs: 90000
    maxBytes: 2097152
    keepBytes: 1048576
    onFinished: function(result) {
      root.busy = false
      if (root.cancellingPoll) {
        root.cancellingPoll = false
        root.actionBusy = false
        root.pendingPoll = false
        root.pendingStatus = false
        var mutation = root.pendingMutation
        root.pendingMutation = null
        if (mutation) {
          Qt.callLater(function() {
            root.executeMutation(mutation.kind, mutation.data)
          })
        }
        return
      }
      var document = null
      if (result.state !== "ok") {
        var failure = root.helperError(result)
        if (failure !== null) root.lastError = failure
        else root.reportFailure(result.state, result.reason, result.stderr)
      } else {
        document = root.parseReport(result.stdout)
        if (document === null) root.lastError = "the helper returned an unreadable report"
        else {
          root.lastError = ""
          root.applyReport(document)
          root.handleReport(document)
        }
      }
      if (root.pendingStatus) {
        root.pendingStatus = false
        Qt.callLater(root.loadStatus)
      } else if (root.pendingPoll) {
        root.pendingPoll = false
        Qt.callLater(root.poll)
      }
    }
    onRunningChanged: {
      if (running) root.busy = true
    }
  }

  Run {
    id: actionRun
    command: []
    deadlineMs: 20000
    maxBytes: 262144
    keepBytes: 262144
    onFinished: function(result) {
      root.actionBusy = false
      actionRun.environment = ({})
      var document = null
      if (typeof result.stdout === "string" && result.stdout.length > 0
          && result.stdout.length <= 1048576) {
        try {
          document = JSON.parse(result.stdout)
        } catch (error) {
          document = null
        }
      }
      var ok = document !== null && document.ok === true
      var message = ""
      if (ok) {
        message = root.actionKind === "add" ? "added"
          : root.actionKind === "update" ? "updated" : "removed"
        root.actionError = ""
      } else {
        if (document && typeof document.error === "string") root.actionError = document.error
        else if (result.state !== "ok") {
          var failure = root.helperError(result)
          if (failure !== null) root.actionError = failure
          else {
            root.reportFailure(result.state, result.reason, result.stderr)
            root.actionError = root.lastError
          }
        } else root.actionError = "could not update the watch list"
        message = root.actionError
        ok = false
      }
      root.actionFinished(ok, message)
      if (ok) {
        root.started = true
        root.loadStatus()
        root.poll()
      }
    }
  }

  Component.onCompleted: loadStatus()
}
