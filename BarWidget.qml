import QtQuick
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Blockwatch bar widget: a read-only watch list for crypto addresses.
//
// The headless Service (declared in the manifest as a second entry point) owns
// polling, unread state and notifications, so every monitor's copy of this
// widget reads the same answer. This file owns the bar surface, tooltip, and
// the add/remove form.
BarWidget {
  id: root
  moduleName: "io.github.ekollof.blockwatch"

  // The host injects the plugin's service into `service`; the lookup keeps
  // working if an older host does not inject it. The empty object below makes
  // every binding safe for the moment before the service is reachable.
  property var service: null
  readonly property var configTarget: root.service
    || (bar && bar.shell ? bar.shell.serviceFor(moduleName) : null)
  readonly property var backend: configTarget || emptyBackend
  property var attachedService: null

  function syncService() {
    if (root.attachedService === root.configTarget) return
    if (root.attachedService && root.attachedService.detachWidget)
      root.attachedService.detachWidget()
    root.attachedService = root.configTarget
    if (root.attachedService && root.attachedService.attachWidget)
      root.attachedService.attachWidget()
  }

  QtObject {
    id: emptyBackend
    property var chains: []
    property var servers: []
    property var fiatCurrencies: []
    property var rates: ({})
    property var addresses: []
    property var errors: []
    property var events: []
    property string checkedAt: ""
    property string warning: ""
    property string lastError: ""
    property bool ready: false
    property bool busy: false
    property int unread: 0
    property bool actionBusy: false
    property string actionError: ""
    readonly property int addressCount: 0
    readonly property bool hasAddresses: false
    readonly property bool notifyEnabled: false
    readonly property int intervalSec: 120
    function poll() {}
    function markRead() {}
    function addAddress() {}
    function updateAddress() {}
    function removeAddress() {}
  }

  Binding {
    target: root.configTarget
    property: "settings"
    value: root.settings
    when: root.configTarget !== null
  }

  Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  // Handed to each row explicitly: inside a delegate, a bare `strings` would
  // resolve to the row's own property, not this Translations object.
  readonly property var stringsApi: strings

  property bool panelOpen: false
  onPanelOpenChanged: {
    if (!panelOpen) {
      root.addViewKey = ""
      root.addApiKey = ""
    }
  }
  property double nowTick: Date.now()

  property bool addOpen: false
  property string addChain: "btc"
  property string addAddress: ""
  property string addLabel: ""
  property string addViewKey: ""
  property string addApiKey: ""
  property string addEndpoint: ""
  property string addXmrMode: "lws"
  property string addServerId: ""
  property string addError: ""
  property string editingId: ""
  property string editingAddress: ""
  property string editingChain: ""
  property bool editingHasViewKey: false
  property bool editingHasApiKey: false

  readonly property bool editing: editingId !== ""
  readonly property bool editTargetChanged: editing
    && (String(addAddress).trim().toLowerCase() !== editingAddress.toLowerCase()
        || addChain !== editingChain)

  onAddAddressChanged: {
    if (addressField.text !== root.addAddress) addressField.text = root.addAddress
  }
  onAddLabelChanged: {
    if (labelField.text !== root.addLabel) labelField.text = root.addLabel
  }
  onAddViewKeyChanged: {
    if (viewKeyField.text !== root.addViewKey) viewKeyField.text = root.addViewKey
  }
  onAddApiKeyChanged: {
    if (apiKeyField.text !== root.addApiKey) apiKeyField.text = root.addApiKey
  }
  onAddEndpointChanged: {
    if (endpointField.text !== root.addEndpoint) endpointField.text = root.addEndpoint
  }
  onAddServerIdChanged: {
    if (serverDropdown.value !== root.addServerId) serverDropdown.value = root.addServerId
  }

  readonly property int unread: backend ? backend.unread : 0
  onUnreadChanged: {
    // A panel that is already on screen is the user reading it; no stale
    // badge should be waiting when it closes.
    if (root.panelOpen && root.unread > 0) root.backend.markRead()
  }
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property color faint: Qt.darker(foreground, 1.7)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string barGlyph: "󰈈"
  readonly property bool verticalBar: bar ? bar.vertical : false

  readonly property string barText: {
    if (root.unread > 0 && !root.verticalBar) return root.barGlyph + " " + root.unread
    return root.barGlyph
  }
  readonly property string tooltipText: {
    if (!backend.ready) return strings.t("tooltip.loading")
    if (backend.lastError !== "") return strings.t("tooltip.error")
    if (backend.addressCount === 0) return strings.t("tooltip.idle")
    if (root.unread > 0) return strings.tn("tooltip.unread", root.unread).arg(root.unread)
    return strings.tn("tooltip.watching", backend.addressCount).arg(backend.addressCount)
  }
  readonly property var selectedChain: root.findChain(root.addChain)
  readonly property string metaText: {
    if (!backend.ready) return strings.t("popup.loading")
    var parts = []
    parts.push(strings.tn("popup.watching", backend.addressCount).arg(backend.addressCount))
    if (backend.busy) parts.push(strings.t("popup.checking"))
    else if (backend.checkedAt !== "") parts.push(strings.t("popup.checked").arg(root.ageText(backend.checkedAt)))
    else parts.push(strings.t("popup.neverChecked"))
    if (backend.errors.length > 0) parts.push(strings.tn("popup.errors", backend.errors.length).arg(backend.errors.length))
    return parts.join(" · ")
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Scriptable lifecycle: refresh now, or open the panel for a screenshot or
  // a keyboard-driven flow.
  ShellIpc {
    target: "io.github.ekollof.blockwatch"

    function open(): string {
      root.openPanel()
      return "ok"
    }
    function close(): string {
      root.panelOpen = false
      return "ok"
    }
    function toggle(): string {
      root.togglePanel()
      return "ok"
    }
    function refresh(): string {
      backend.poll()
      return "ok"
    }
    function markRead(): string {
      backend.markRead()
      return "ok"
    }
    function edit(id: string): string {
      var entry = root.addressById(String(id))
      if (!entry) return "unknown"
      root.openEditForm(entry)
      return "ok"
    }
    function openAll(): string {
      root.broadcast("openPanel")
      return "ok"
    }
    function closeAll(): string {
      root.broadcast("close")
      return "ok"
    }
  }

  function close() {
    panelOpen = false
  }

  function findChain(id) {
    var list = backend.chains || []
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === id) return list[i]
    }
    return null
  }

  function findServer(id) {
    var list = backend.servers || []
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].id) === String(id)) return list[i]
    }
    return null
  }

  function addressById(id) {
    var list = backend.addresses || []
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].id) === String(id)) return list[i]
    }
    return null
  }

  readonly property var serverOptions: {
    var out = []
    var list = backend.servers || []
    for (var i = 0; i < list.length; i++) {
      var server = list[i]
      if (!server || typeof server.id !== "string") continue
      out.push({ value: server.id, label: Model.plain(server.label || server.endpoint || server.id) })
    }
    out.push({ value: "__custom__", label: strings.t("popup.serverCustom") })
    return out
  }
  readonly property var selectedServer: root.findServer(root.addServerId)
  readonly property var fiatOptions: {
    var out = []
    var list = backend.fiatCurrencies || []
    for (var i = 0; i < list.length; i++) {
      var item = list[i]
      if (!item || typeof item.code !== "string") continue
      out.push({ value: item.code, label: item.code })
    }
    if (out.length === 0) out.push({ value: "USD", label: "USD" })
    return out
  }
  readonly property string fiatCurrency: {
    var value = String(root.setting("fiatCurrency", "USD")).toUpperCase()
    var list = backend.fiatCurrencies || []
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].code) === value) return value
    }
    return "USD"
  }

  onFiatCurrencyChanged: {
    if (fiatPicker.value !== root.fiatCurrency) fiatPicker.value = root.fiatCurrency
  }

  function setFiat(value) {
    var code = String(value || "").toUpperCase()
    if (code === "") return
    var entry = { id: moduleName }
    var current = root.settings || {}
    for (var name in current) {
      if (name !== "id") entry[name] = current[name]
    }
    entry.fiatCurrency = code
    var api = bar ? bar.shell : null
    if (api && api.updateEntryInline) {
      if (!api.updateEntryInline(moduleName, entry)) {
        fiatPicker.value = root.fiatCurrency
        return
      }
      root.settings = entry
      Qt.callLater(backend.poll)
    } else {
      fiatPicker.value = root.fiatCurrency
    }
  }

  function syncServerSelection() {
    if (root.addChain !== "xmr") {
      root.addServerId = ""
      return
    }
    var list = backend.servers || []
    for (var i = 0; i < list.length; i++) {
      if (list[i].endpoint === root.addEndpoint && list[i].mode === root.addXmrMode) {
        root.addServerId = list[i].id
        return
      }
    }
    root.addServerId = "__custom__"
  }

  function selectServer(id) {
    var previous = root.addServerId
    root.addServerId = id
    if (id === "__custom__") {
      if (previous !== "__custom__") root.addEndpoint = ""
      return
    }
    var server = root.findServer(id)
    if (!server) return
    root.addXmrMode = server.mode
    root.addEndpoint = server.endpoint
  }

  function setXmrMode(mode) {
    if (root.addXmrMode === mode) return
    root.addXmrMode = mode
    var list = backend.servers || []
    for (var i = 0; i < list.length; i++) {
      if (list[i].mode === mode) {
        root.selectServer(list[i].id)
        return
      }
    }
    root.addServerId = "__custom__"
    root.addEndpoint = mode === "wallet-rpc" ? "http://127.0.0.1:18083/json_rpc" : ""
  }

  function ageText(iso) {
    var age = Model.age(iso, root.nowTick)
    if (!age) return ""
    if (age.unit === "now") return strings.t("time.now")
    return strings.tn("time." + age.unit, age.value).arg(age.value)
  }

  function chainPlaceholder(id) {
    switch (id) {
    case "btc": return "bc1… / 1… / 3…"
    case "ltc": return "ltc1… / L…"
    case "doge": return "D…"
    case "eth": return "0x…"
    case "sol": return "base58 address"
    case "xmr": return "4… (main address)"
    case "lightning": return "name@blink.sv"
    }
    return ""
  }

  function selectChain(id) {
    root.addChain = id
    root.addError = ""
    if (id !== "xmr") {
      root.addViewKey = ""
      root.addXmrMode = "lws"
      root.addEndpoint = ""
      root.addServerId = ""
    }
    if (id !== "lightning") root.addApiKey = ""
    if (id !== "xmr") return
    root.addXmrMode = "lws"
    root.addServerId = ""
    root.addEndpoint = ""
    root.syncServerSelection()
    if (root.addServerId === "__custom__") {
      var server = root.findServer("lws-monero-fail")
      if (server) root.selectServer(server.id)
    }
  }

  function resetForm() {
    root.addAddress = ""
    root.addLabel = ""
    root.addViewKey = ""
    root.addApiKey = ""
    root.addEndpoint = ""
    root.addError = ""
    root.addXmrMode = "lws"
    root.addServerId = ""
    root.editingId = ""
    root.editingAddress = ""
    root.editingChain = ""
    root.editingHasViewKey = false
    root.editingHasApiKey = false
  }

  function openAddForm() {
    root.resetForm()
    root.addOpen = true
    root.addError = ""
  }

  function openEditForm(entry) {
    if (!entry) return
    root.editingId = String(entry.id || "")
    root.editingAddress = String(entry.address || "")
    root.editingChain = String(entry.chain || "btc")
    root.editingHasViewKey = entry.hasViewKey === true
    root.editingHasApiKey = entry.hasApiKey === true
    root.addChain = String(entry.chain || "btc")
    root.addAddress = String(entry.address || "")
    root.addLabel = String(entry.label || "")
    root.addViewKey = ""
    root.addApiKey = ""
    root.addXmrMode = String(entry.mode || "lws")
    root.addEndpoint = String(entry.endpoint || "")
    root.addServerId = ""
    root.addError = ""
    root.addOpen = true
    if (root.addChain === "xmr") root.syncServerSelection()
  }

  function cancelForm() {
    root.addOpen = false
    root.resetForm()
    root.addError = ""
  }

  function submitAdd() {
    var address = String(root.addAddress || "").trim()
    if (address === "") {
      root.addError = strings.t("popup.addressMissing")
      return
    }
    var mode = root.addChain === "xmr" ? root.addXmrMode : ""
    if (root.addChain === "xmr" && String(root.addEndpoint || "").trim() === "") {
      root.addError = strings.t("popup.endpointMissing")
      return
    }
    var keepsKey = root.editing && root.editingHasViewKey
      && !root.editTargetChanged && root.addXmrMode === "lws"
    if (root.addChain === "xmr" && mode === "lws" && !keepsKey
        && String(root.addViewKey || "").trim() === "") {
      root.addError = strings.t("popup.viewKeyMissing")
      return
    }
    var keepsApiKey = root.editing && root.editingHasApiKey && !root.editTargetChanged
    if (root.addChain === "lightning" && !keepsApiKey
        && String(root.addApiKey || "").trim() === "") {
      root.addError = strings.t("popup.apiKeyMissing")
      return
    }
    if (root.editing) {
      backend.updateAddress(root.editingId, root.addChain, address, root.addLabel,
                            root.addViewKey, mode, root.addEndpoint,
                            root.addApiKey)
    } else {
      backend.addAddress(root.addChain, address, root.addLabel,
                         root.addViewKey, mode, root.addEndpoint,
                         root.addApiKey)
    }
  }

  function openPanel() {
    panelOpen = true
    backend.markRead()
    addError = ""
  }

  function togglePanel() {
    if (panelOpen) panelOpen = false
    else openPanel()
  }

  Component.onCompleted: Qt.callLater(root.syncService)
  onConfigTargetChanged: Qt.callLater(root.syncService)
  Component.onDestruction: {
    if (root.attachedService && root.attachedService.detachWidget)
      root.attachedService.detachWidget()
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barText
    tooltipText: root.tooltipText
    active: root.unread > 0
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton) {
        backend.poll()
        return
      }
      root.togglePanel()
    }
  }

  // Keep relative times fresh while the panel is readable.
  Timer {
    interval: 60000
    running: root.panelOpen
    repeat: true
    onTriggered: root.nowTick = Date.now()
  }

  Connections {
    target: root.backend
    ignoreUnknownSignals: true
    function onActionFinished(ok, message) {
      if (ok) {
        root.resetForm()
        root.addOpen = false
      } else {
        root.addError = message
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.panelOpen
    focusTarget: contentColumn
    contentWidth: panel.fittedContentWidth(Style.space(390))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    Flickable {
      id: panelScroll
      anchors.fill: parent
      contentWidth: width
      contentHeight: contentColumn.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      interactive: contentHeight > height

      Column {
        id: contentColumn
        width: panelScroll.width
      focus: true
      spacing: Style.space(10)
      Keys.onEscapePressed: root.close()

      Row {
        width: parent.width
        spacing: Style.space(8)

        Column {
          width: Math.max(0, parent.width - fiatPicker.width - refreshButton.width - Style.space(16))
          spacing: Style.space(2)

          Text {
            textFormat: Text.PlainText
            text: strings.t("popup.title")
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.metaText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        Dropdown {
          id: fiatPicker
          width: Style.space(76)
          value: root.fiatCurrency
          options: root.fiatOptions
          showLabel: false
          rowHeight: Style.space(26)
          popupRowHeight: Style.space(24)
          foreground: root.foreground
          fontFamily: root.fontFamily
          onChanged: function(value) {
            root.setFiat(value)
          }
        }

        Button {
          id: refreshButton
          iconText: "󰑐"
          tooltipText: strings.t("popup.refresh")
          foreground: root.foreground
          fontFamily: root.fontFamily
          enabled: !backend.busy
          opacity: enabled ? 1 : 0.5
          onClicked: backend.poll()
        }
      }

      BorderSurface {
        width: parent.width
        visible: backend.lastError !== "" || backend.warning !== ""
        implicitHeight: bannerText.implicitHeight + Style.space(12)
        color: Style.normalFillFor(Commons.Color.urgent, Commons.Color.urgent)
        borderSpec: Border.controlSpec("normal", Commons.Color.urgent, Commons.Color.urgent)
        radius: Style.cornerRadius

        Text {
          id: bannerText
          textFormat: Text.PlainText
          anchors.fill: parent
          anchors.margins: Style.space(6)
          text: backend.lastError !== "" ? Model.plain(backend.lastError) : Model.plain(backend.warning)
          color: Commons.Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          elide: Text.ElideRight
          maximumLineCount: 3
        }
      }

      Column {
        width: parent.width
        spacing: Style.space(4)
        visible: backend.ready && backend.addressCount === 0

        Text {
          textFormat: Text.PlainText
          text: strings.t("popup.emptyTitle")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          text: strings.t("popup.emptyBody")
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }
      }

      Column {
        id: addressList
        width: parent.width
        spacing: Style.space(6)

        Repeater {
          model: backend.addresses

          delegate: AddressRow {
            width: addressList.width
            entry: modelData
            strings: root.stringsApi
            foreground: root.foreground
            fontFamily: root.fontFamily
            nowMs: root.nowTick
            removing: backend.actionBusy
            onRemoveRequested: function(id) {
              backend.removeAddress(id)
            }
            onEditRequested: function(entry) {
              root.openEditForm(entry)
            }
          }
        }
      }

      Item {
        width: parent.width
        height: addToggle.implicitHeight

        Button {
          id: addToggle
          anchors.left: parent.left
          text: root.addOpen ? strings.t("popup.cancel") : strings.t("popup.add")
          iconText: root.addOpen ? "" : "󰐕"
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: !root.addOpen
          onClicked: {
            if (root.addOpen) root.cancelForm()
            else root.openAddForm()
          }
        }
      }

      Column {
        id: addForm
        width: parent.width
        visible: root.addOpen
        spacing: Style.space(6)

        PanelSectionHeader {
          visible: root.editing
          text: strings.t("popup.editTitle")
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        PanelSectionHeader {
          text: strings.t("popup.chain")
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Flow {
          width: parent.width
          spacing: Style.space(4)

          Repeater {
            model: backend.chains

            delegate: Button {
              text: String(modelData.symbol || "")
              tooltipText: String(modelData.name || "")
              selected: root.addChain === modelData.id
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(7)
              verticalPadding: Style.space(4)
              onClicked: root.selectChain(modelData.id)
            }
          }
        }

        TextField {
          id: addressField
          width: parent.width
          placeholderText: root.chainPlaceholder(root.addChain)
          text: root.addAddress
          foreground: root.foreground
          onTextChanged: root.addAddress = text
          Keys.onReturnPressed: root.submitAdd()
          Keys.onEnterPressed: root.submitAdd()
        }

        TextField {
          id: labelField
          width: parent.width
          placeholderText: strings.t("popup.label")
          text: root.addLabel
          foreground: root.foreground
          onTextChanged: root.addLabel = text
          Keys.onReturnPressed: root.submitAdd()
          Keys.onEnterPressed: root.submitAdd()
        }

        Row {
          width: parent.width
          spacing: Style.space(4)
          visible: root.addChain === "xmr"

          Button {
            text: strings.t("popup.modeLws")
            selected: root.addXmrMode === "lws"
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            onClicked: root.setXmrMode("lws")
          }

          Button {
            text: strings.t("popup.modeRpc")
            selected: root.addXmrMode === "wallet-rpc"
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            onClicked: root.setXmrMode("wallet-rpc")
          }
        }

        Dropdown {
          id: serverDropdown
          width: parent.width
          visible: root.addChain === "xmr"
          label: strings.t("popup.server")
          value: root.addServerId
          options: root.serverOptions
          foreground: root.foreground
          fontFamily: root.fontFamily
          showLabel: true
          onChanged: function(value) {
            root.selectServer(value)
          }
        }

        TextField {
          id: endpointField
          width: parent.width
          visible: root.addChain === "xmr"
          placeholderText: root.addXmrMode === "wallet-rpc"
            ? "http://127.0.0.1:18083/json_rpc"
            : "https://lws.example"
          foreground: root.foreground
          text: root.addEndpoint
          onTextChanged: {
            root.addEndpoint = text
            if (root.selectedServer && text !== root.selectedServer.endpoint)
              root.addServerId = "__custom__"
            if (text.trim().replace(/\/$/, "").endsWith("/json_rpc")
                && root.addXmrMode !== "wallet-rpc")
              root.addXmrMode = "wallet-rpc"
          }
          Keys.onReturnPressed: root.submitAdd()
          Keys.onEnterPressed: root.submitAdd()
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: root.addChain === "xmr" && !!(root.selectedServer && root.selectedServer.note)
          text: root.selectedServer ? Model.plain(root.selectedServer.note || "") : ""
          color: root.faint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: root.addChain === "xmr"
            && (root.addServerId === "__custom__"
                || !!(root.selectedServer && root.selectedServer.custom))
          text: strings.t("popup.customServerHint")
          color: root.faint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: root.addChain === "xmr"
          text: strings.t("popup.serverHint")
          color: root.faint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        TextField {
          id: viewKeyField
          width: parent.width
          visible: root.addChain === "xmr" && root.addXmrMode === "lws"
          placeholderText: strings.t("popup.viewKey")
          password: true
          foreground: root.foreground
          text: root.addViewKey
          onTextChanged: root.addViewKey = text
          Keys.onReturnPressed: root.submitAdd()
          Keys.onEnterPressed: root.submitAdd()
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: root.addChain === "xmr"
          text: root.addXmrMode === "wallet-rpc"
            ? strings.t("popup.rpcHint")
            : (root.editing && root.editingHasViewKey && !root.editTargetChanged
               ? strings.t("popup.viewKeyKeep") : strings.t("popup.viewKeyHint"))
          color: root.addChain === "xmr" && root.addXmrMode === "lws"
            && !(root.editing && root.editingHasViewKey && !root.editTargetChanged)
            ? Commons.Color.urgent : root.faint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        TextField {
          id: apiKeyField
          width: parent.width
          visible: root.addChain === "lightning"
          placeholderText: strings.t("popup.apiKey")
          password: true
          foreground: root.foreground
          text: root.addApiKey
          onTextChanged: root.addApiKey = text
          Keys.onReturnPressed: root.submitAdd()
          Keys.onEnterPressed: root.submitAdd()
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: root.addChain === "lightning"
          text: root.editing && root.editingHasApiKey && !root.editTargetChanged
            ? strings.t("popup.apiKeyKeep") : strings.t("popup.apiKeyHint")
          color: root.addChain === "lightning"
            && !(root.editing && root.editingHasApiKey && !root.editTargetChanged)
            ? Commons.Color.urgent : root.faint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: !!(root.selectedChain && root.selectedChain.note)
          text: root.selectedChain ? Model.plain(root.selectedChain.note || "") : ""
          color: root.faint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Button {
          text: backend.actionBusy
            ? (root.editing ? strings.t("popup.saving") : strings.t("popup.adding"))
            : (root.editing ? strings.t("popup.save") : strings.t("popup.addAction"))
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          enabled: !backend.actionBusy && String(root.addAddress || "").trim() !== ""
          opacity: enabled ? 1 : 0.5
          onClicked: root.submitAdd()
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: root.addError !== ""
          text: Model.plain(root.addError)
          color: Commons.Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: strings.t("popup.footer")
        color: root.faint
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: !!(backend.rates && backend.rates.error)
        text: strings.t("popup.ratesUnavailable")
        color: Commons.Color.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
      }
    }
  }
}
