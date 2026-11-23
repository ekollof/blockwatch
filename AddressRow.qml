import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// One watched address inside the panel: what it is, what it holds, and the one
// action that is allowed here — stop watching. There is no send, receive or
// key-management action anywhere in this plugin.
Item {
  id: row

  property var entry: null
  property var strings: null
  property color foreground: Commons.Color.foreground
  property string fontFamily: Style.font.family
  property bool removing: false
  property double nowMs: Date.now()

  signal removeRequested(string id)
  signal editRequested(var entry)

  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property color faint: Qt.darker(foreground, 1.7)
  readonly property var lastAge: Model.age(entry ? entry.lastEventAt : null, row.nowMs)
  readonly property bool isError: !!entry && entry.status === "error"
  readonly property bool isPending: !!entry && entry.status === "pending"
  readonly property string labelText: entry ? Model.displayLabel(entry) : ""
  readonly property string chainGlyph: entry ? Model.chainGlyph(entry.chain) : ""

  implicitWidth: parent ? parent.width : Style.space(320)
  implicitHeight: content.implicitHeight + Style.space(16)

  function ageText(age) {
    if (!age) return ""
    if (!strings) return ""
    if (age.unit === "now") return strings.t("time.now")
    return strings.tn("time." + age.unit, age.value).arg(age.value)
  }

  function balanceText() {
    if (!strings || !entry || entry.balance === null || entry.balance === undefined) return ""
    var prefix = entry.balanceLabel === "received" ? strings.t("popup.received") + " " : ""
    return prefix + String(entry.balance) + " " + String(entry.symbol || "")
  }

  BorderSurface {
    anchors.fill: parent
    color: hover.hovered ? Style.hoverFillFor(row.foreground, Commons.Color.accent)
                         : Style.normalFillFor(row.foreground, Commons.Color.accent)
    borderSpec: Border.controlSpec("normal", row.foreground, Commons.Color.accent)
    radius: Style.cornerRadius
  }

  // The whole row opens the edit form; the trash button stays on top and
  // consumes its own click. A pencil appears beside the label on hover.
  MouseArea {
    id: rowMouse
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: row.editRequested(row.entry)
  }

  Row {
    id: content
    anchors.fill: parent
    anchors.margins: Style.space(8)
    spacing: Style.space(8)

    BorderSurface {
      width: Style.space(28)
      height: Style.space(28)
      radius: Style.space(14)
      anchors.verticalCenter: parent.verticalCenter
      color: Style.normalFillFor(row.foreground, Commons.Color.accent)
      borderSpec: Border.controlSpec("normal", row.foreground, Commons.Color.accent)

      Text {
        textFormat: Text.PlainText
        anchors.centerIn: parent
        text: row.chainGlyph
        color: row.foreground
        font.family: row.fontFamily
        font.pixelSize: Style.font.body
      }
    }

    Column {
      width: Math.max(0, parent.width - Style.space(28) - actions.width - Style.space(16))
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(1)

      Row {
        width: parent.width
        spacing: Style.space(6)

        Text {
          textFormat: Text.PlainText
          text: row.labelText
          color: row.isError ? Commons.Color.urgent : row.foreground
          font.family: row.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          elide: Text.ElideRight
          width: Math.min(implicitWidth, parent.width - (pendingIcon.visible ? pendingIcon.width + Style.space(6) : 0))
        }

        Text {
          id: pendingIcon
          textFormat: Text.PlainText
          visible: row.isPending
          text: "󰅐"
          color: row.faint
          font.family: row.fontFamily
          font.pixelSize: Style.font.caption
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      // The label is optional, so the address stays readable underneath it.
      Text {
        textFormat: Text.PlainText
        text: row.entry ? row.entry.address : ""
        visible: !!(row.entry && row.entry.label)
        color: row.faint
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideMiddle
        width: parent.width
      }

      Text {
        textFormat: Text.PlainText
        text: row.balanceText()
        visible: text !== ""
        color: row.dim
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        width: parent.width
      }

      Text {
        textFormat: Text.PlainText
        visible: row.isError
        text: row.entry ? Model.plain(row.entry.error || "") : ""
        color: Commons.Color.urgent
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        width: parent.width
        maximumLineCount: 3
        elide: Text.ElideRight
      }

      Text {
        textFormat: Text.PlainText
        visible: !row.isError && !!(row.entry && row.entry.initialized)
        text: row.strings ? row.strings.t("popup.baseline") : ""
        color: row.faint
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        width: parent.width
        maximumLineCount: 2
        elide: Text.ElideRight
      }

      Text {
        textFormat: Text.PlainText
        visible: !row.isError && !!(row.entry && row.entry.note)
        text: row.entry ? Model.plain(row.entry.note || "") : ""
        color: row.faint
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        width: parent.width
        maximumLineCount: 2
        elide: Text.ElideRight
      }

      Text {
        textFormat: Text.PlainText
        visible: !row.isError && !(row.entry && (row.entry.note || row.entry.initialized)) && row.lastAge !== null
        text: row.strings ? row.strings.t("popup.lastEvent").arg(row.ageText(row.lastAge)) : ""
        color: row.faint
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        width: parent.width
      }
    }

    Row {
      id: actions
      width: implicitWidth
      height: implicitHeight
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(3)

      Text {
        textFormat: Text.PlainText
        text: "󰏫"
        color: row.faint
        font.family: row.fontFamily
        font.pixelSize: Style.font.body
        anchors.verticalCenter: parent.verticalCenter
      }

      Button {
        iconText: "󰆴"
        tooltipText: row.strings ? row.strings.t("popup.remove") : ""
        foreground: Commons.Color.urgent
        fontSize: Style.font.caption
        horizontalPadding: Style.space(5)
        verticalPadding: Style.space(3)
        enabled: !row.removing
        opacity: enabled ? 0.8 : 0.4
        onClicked: row.removeRequested(row.entry ? row.entry.id : "")
      }
    }
  }

  HoverHandler {
    id: hover
  }
}
